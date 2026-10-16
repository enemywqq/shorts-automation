"""Оркестратор: python -m app {process,upload,status,retry}."""
import argparse
import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time as clock
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlparse

from . import config

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

log = logging.getLogger("app")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id      INTEGER PRIMARY KEY,
    path    TEXT NOT NULL,
    caption TEXT NOT NULL,
    source  TEXT NOT NULL,
    slot    TEXT,
    status  TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'uploaded', 'failed', 'unknown')),
    error   TEXT
)
"""

# Признак PublishUncertainError в выводе autotok: пост мог уйти, а мог и нет.
UNCERTAIN_MARKER = "check the account"


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(SCHEMA)
    return db


# -- process --------------------------------------------------------------------


def resolve_source(raw: str) -> "tuple[str, bool] | None":
    """(абсолютный путь или URL, это файл?) либо None."""
    if Path(raw).is_file():
        return str(Path(raw).resolve()), True
    if raw.startswith(("http://", "https://")):
        return raw, False
    log.error("Файл не найден: %s", raw)
    return None


def url_stem(url: str) -> str:
    """id ролика YouTube (watch?v=, youtu.be/, shorts/) или последний сегмент пути."""
    parsed = urlparse(url)
    return parse_qs(parsed.query).get("v", [""])[0] or parsed.path.rstrip("/").rpartition("/")[2]


def source_id(source: str, is_file: bool) -> str:
    # Только ASCII: путь к папке уходит в OpenCV, а тот на Windows не всегда
    # открывает файлы с кириллицей.
    stem = Path(source).stem if is_file else url_stem(source)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:40] or "video"
    return f"{stem}_{hashlib.sha1(source.encode()).hexdigest()[:8]}"


Cue = tuple[float, float, str]

# Координаты в кадре 1080x1920, libass масштабирует их под реальный размер клипа.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Arial,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,2,{ml},{mr},0,1
Style: Title,{font},{size},{color},{color},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},0,8,{tm},{tm},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
# Около 20 символов на строку при шрифте 80 в безопасной зоне: так кусок не вылезает за две строки.
SUB_MAX_CHARS = 40
# ponytail: ширина текста не измеряется, а оценивается по числу символов.
# Подобрано на Russo One с обводкой: при шрифте 100 в строку шириной 960 влезает около 18 символов.
TITLE_LINE_CHARS_AT_100 = 18

# Геометрия кадра 1080x1920. Текст стоит внутри безопасной зоны TikTok и центрируется по ней.
SAFE_WIDTH = 1080 - config.SAFE_LEFT - config.SAFE_RIGHT
SAFE_CX = config.SAFE_LEFT + SAFE_WIDTH // 2
# Заголовок по центру кадра: зона несимметрична (справа кнопки), поэтому оба поля берём
# по большему боковому отступу, иначе текст съезжает влево.
TITLE_MARGIN = max(config.SAFE_LEFT, config.SAFE_RIGHT)
TITLE_WIDTH = 1080 - 2 * TITLE_MARGIN
TEXT_TOP = config.SAFE_TOP + 20             # верх заголовка
TEXT_BOTTOM = 1920 - config.SAFE_BOTTOM - 20  # низ субтитров
FIT_TOP = TEXT_TOP + 240                    # картинка fit ниже места под заголовок в две строки
SPLIT_CAM_H = 768                           # в split вебка 1080x768 сверху, экран 1080x1152 снизу


def read_srt(path: Path) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig")):
        m = re.search(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)\s*\n(.*)", block, re.S)
        if m:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups()[:8])
            cues.append((h1 * 3600 + m1 * 60 + s1 + ms1 / 1000, h2 * 3600 + m2 * 60 + s2 + ms2 / 1000,
                         " ".join(m.group(9).split())))
    return cues


def split_text(text: str) -> list[str]:
    parts, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > SUB_MAX_CHARS:
            parts.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return parts + [cur] if cur else parts


def clip_cues(cues: list[Cue], start: float, end: float) -> list[Cue]:
    """Реплики внутри [start, end], сдвинутые к нулю и порезанные на куски не длиннее двух строк."""
    out = []
    for a, b, text in cues:
        a, b = max(a, start) - start, min(b, end) - start
        parts = split_text(text)
        if b <= a or not parts:
            continue
        # Время реплики делим между кусками пропорционально длине текста.
        total, t = sum(map(len, parts)), a
        for part in parts:
            dur = (b - a) * len(part) / total
            out.append((t, t + dur, part))
            t += dur
    return out


def ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def title_layout(title: str) -> tuple[str, int]:
    """Текст заголовка для ASS и размер шрифта. Длинный заголовок режется на две строки по словам
    ближе к середине, шрифт уменьшается, пока длинная строка не влезет в безопасную зону."""
    per_line = TITLE_LINE_CHARS_AT_100 * 100 / config.TITLE_SIZE * TITLE_WIDTH / 960
    words = title.split()
    if len(title) <= per_line or len(words) < 2:
        lines = [title]
    else:
        cut = min(range(1, len(words)),
                  key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    longest = max(map(len, lines))
    size = config.TITLE_SIZE if longest <= per_line else int(config.TITLE_SIZE * per_line / longest)
    return r"\N".join(map(ass_text, lines)), size


def write_ass(path: Path, cues: list[Cue], title: str, layout: str, duration: float) -> None:
    lines = [f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Sub,,0,0,0,,{SUB_POS[layout]}{ass_text(text)}"
             for a, b, text in cues]
    if title:
        shown = config.TITLE_SECONDS or duration
        text, size = title_layout(title)
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(shown)},Title,,0,0,0,,"
                     f"{TITLE_POS[layout]}{{\\fs{size}}}{text}")
    header = ASS_HEADER.format(font=config.TITLE_FONT, size=config.TITLE_SIZE, color=config.TITLE_COLOR,
                               outline=config.TITLE_OUTLINE, ml=config.SAFE_LEFT, mr=config.SAFE_RIGHT,
                               tm=TITLE_MARGIN)
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def nearest(points: list[float], t: float) -> float:
    """Ближайшая к t точка не дальше SNAP_MAX, иначе само t."""
    near = [p for p in points if abs(p - t) <= config.SNAP_MAX]
    return min(near, key=lambda p: abs(p - t)) if near else t


def snap(cues: list[Cue], start: float, end: float) -> tuple[float, float]:
    """Подтягивает начало к ближайшему началу реплики, конец к ближайшему концу, добавляет запас."""
    start = nearest([a for a, _, _ in cues], start)
    end = nearest([b for _, b, _ in cues], end)
    return max(0.0, start - config.SNAP_PAD), end + config.SNAP_PAD


# Видеофильтры макетов: вход [0:v], кадр 1080x1920. Поля в {} подставляются в render.
# ponytail: fit и позиция субтитров в нём рассчитаны на горизонтальный исходник 16:9.
LAYOUTS = {
    # Фон размываем на копии 270x480: boxblur на полном кадре в разы медленнее.
    "fit": "[0:v]split[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
           "boxblur=10:2,scale=1080:1920[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:" + str(FIT_TOP),
    "split": "[0:v]split[a][b];[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
             f"scale=1080:{SPLIT_CAM_H}:force_original_aspect_ratio=increase,crop=1080:{SPLIT_CAM_H}[cam];"
             # Экран чуть приближен: при полной высоте в кадр попадал край вебки.
             f"[b]crop=ih*0.9*1080/{1920 - SPLIT_CAM_H}:ih*0.9,scale=1080:{1920 - SPLIT_CAM_H}[scr];"
             "[cam][scr]vstack",
    "crop": "[0:v]scale=-2:1920,crop=1080:1920",
    # Окно 9:16 во всю высоту, центр по x следует за лицом: {cx} это выражение от t.
    "face": "[0:v]crop=w=ih*9/16:h=ih:x='clip(({cx})*iw-ow/2,0,iw-ow)':y=0,scale=1080:1920",
}
# Теги позиции. fit: заголовок над картинкой, субтитры под ней. split: заголовок под вебкой,
# субтитры под заголовком, выше прицела (центр экрана, y=1344) и выше интерфейса TikTok внизу.
# crop и face: заголовок сверху, субтитры снизу, оба внутри безопасной зоны.
TOP_TITLE = rf"{{\an8\pos(540,{TEXT_TOP})}}"
BOTTOM_SUB = rf"{{\an2\pos({SAFE_CX},{TEXT_BOTTOM})}}"
SUB_POS = {"fit": rf"{{\an8\pos({SAFE_CX},{FIT_TOP + 640})}}",
           "split": rf"{{\an8\pos({SAFE_CX},{SPLIT_CAM_H + 250})}}",
           "crop": BOTTOM_SUB, "face": BOTTOM_SUB}
TITLE_POS = {"fit": TOP_TITLE, "split": rf"{{\an8\pos(540,{SPLIT_CAM_H + 22})}}",
             "crop": TOP_TITLE, "face": TOP_TITLE}


def cam_rect(face: list[float], size: list[float]) -> dict:
    """Рамка вебки вокруг лица: высота AUTO_CAM_SCALE лиц, пропорции верхней панели split."""
    h = min(1.0, face[3] * config.AUTO_CAM_SCALE)
    w = min(1.0, h * size[1] / size[0] * 1080 / SPLIT_CAM_H)
    x = min(max(face[0] - w / 2, 0.0), 1 - w)
    y = min(max(face[1] - h * config.AUTO_CAM_FACE_Y, 0.0), 1 - h)
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def track_expr(samples: list) -> str:
    """Выражение ffmpeg от t: сглаженный центр крупнейшего лица по x, линейно между замерами."""
    xs, last = [], None
    for _, boxes in samples:
        if boxes:
            last = max(boxes, key=lambda f: f[3])[0]
        xs.append(last)
    first = next(x for x in xs if x is not None)
    xs = [first if x is None else x for x in xs]
    k = config.AUTO_SMOOTH // 2
    xs = [sum(xs[max(0, i - k):i + k + 1]) / len(xs[max(0, i - k):i + k + 1]) for i in range(len(xs))]
    ts = [t for t, _ in samples]
    expr = f"{xs[-1]:.4f}"
    for i in range(len(xs) - 2, -1, -1):
        expr = (f"if(lt(t,{ts[i + 1]}),{xs[i]:.4f}+({xs[i + 1] - xs[i]:.4f})*(t-{ts[i]})"
                f"/{ts[i + 1] - ts[i]:.2f},{expr})")
    return expr


def pick_layout(samples: list, size: list[float]) -> tuple[str, dict, str]:
    """(раскладка, поля фильтра, причина для лога) по замерам лиц одного клипа."""
    faces = [(t, f) for t, boxes in samples for f in boxes]
    if not faces:
        return "fit", {}, "лиц нет"
    n = len(samples)

    def same_spot(a: list[float], b: list[float]) -> bool:
        return abs(a[0] - b[0]) <= config.AUTO_STABLE and abs(a[1] - b[1]) <= config.AUTO_STABLE

    def times(f: list[float]) -> set:
        return {t for t, g in faces if same_spot(f, g)}

    def motion(f: list[float]) -> float:
        moves = [g[4] for _, g in faces if same_spot(f, g) and g[4] >= 0]
        return median(moves) if moves else 0.0

    # Вебка: из лиц, которые держатся на одном месте, самое живое. Аватар в оверлее тоже стоит
    # на месте, но меняется реже лица в вебке, а совсем неподвижная картинка отсекается AUTO_STILL.
    stable = [f for _, f in faces
              if len(times(f)) / n >= config.AUTO_MIN_SHARE and motion(f) >= config.AUTO_STILL]
    if stable:
        anchor = max(stable, key=motion)
        cam = [median(g[i] for _, g in faces if same_spot(anchor, g)) for i in range(4)]
        share = len(times(anchor)) / n
    if stable and cam[3] < config.AUTO_BIG_FACE and abs(cam[0] - 0.5) >= config.AUTO_EDGE:
        rect = cam_rect(cam, size)
        return "split", rect, (f"лицо высотой {cam[3]:.2f} стоит у края (x {cam[0]:.2f}, y {cam[1]:.2f}) "
                               f"в {share:.0%} замеров, рамка вебки {rect}")

    biggest = [max(boxes, key=lambda f: f[3]) for _, boxes in samples if boxes]
    h, cx, share = median(f[3] for f in biggest), median(f[0] for f in biggest), len(biggest) / n
    if share >= config.AUTO_MIN_SHARE and h >= config.AUTO_BIG_FACE and abs(cx - 0.5) <= config.AUTO_CENTER:
        return "face", {"cx": track_expr(samples)}, (f"крупное лицо (высота {h:.2f}, x {cx:.2f}) "
                                                     f"в {share:.0%} замеров")
    # IRL в движении: человек ходит, медиана размера и места ничего не говорит, поэтому
    # считаем замеры, где лицо заметное и не у края.
    walking = sum(f[3] >= config.AUTO_SMALL_FACE and abs(f[0] - 0.5) < config.AUTO_EDGE for f in biggest) / n
    if walking >= config.AUTO_IRL_SHARE:
        return "face", {"cx": track_expr(samples)}, (f"лицо двигается, но в {walking:.0%} замеров "
                                                     f"заметное и не у края")
    return "fit", {}, (f"крупнейшее лицо в {share:.0%} замеров, высота {h:.2f}, x {cx:.2f}, "
                       f"заметное и не у края только в {walking:.0%}: не вебка и не IRL")


def detect_faces(video: Path, spans: list[tuple[float, float]]) -> "dict | None":
    """Замеры лиц по клипам из app/faces.py или None, если детектор упал."""
    opts = {"spans": spans, "step": config.AUTO_SAMPLE_SECONDS, "score": config.AUTO_FACE_SCORE,
            "ffmpeg": config.FFMPEG}
    proc = subprocess.run([str(config.GENERATOR_PYTHON), str(Path(__file__).with_name("faces.py")),
                           str(config.FACE_MODEL), str(video), json.dumps(opts)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log.error("Детектор лиц завершился с кодом %d:\n%s", proc.returncode, proc.stderr.strip()[-2000:])
        return None
    return json.loads(proc.stdout)


def render(video: Path, start: float, end: float, dst: Path, layout: str,
           params: dict, ass: "Path | None") -> None:
    # fps первым фильтром: дальше вся цепочка, включая ass, обрабатывает не больше OUTPUT_FPS кадров в секунду.
    graph = LAYOUTS[layout].format(**params).replace(
        "[0:v]", f"[0:v]fps='min(source_fps,{config.OUTPUT_FPS})',", 1) + ",setsar=1"
    if ass:
        # Имя без пути, cwd в папке клипа, fontsdir относительный: в фильтрах ffmpeg
        # двоеточие из C:\ надо экранировать.
        fonts = Path(os.path.relpath(config.FONTS_DIR, dst.parent)).as_posix()
        graph += f",ass={ass.name}:fontsdir={fonts}"
    cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", str(video), "-filter_complex", graph + "[v]", "-map", "[v]", "-map", "0:a?",
           "-af", f"loudnorm=I={config.LOUDNESS}:TP=-1.5:LRA=11", "-ar", "48000",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    subprocess.run(cmd, check=True, cwd=dst.parent)


# Этап генератора и строка его вывода, на которой этап заканчивается. Последний длится до выхода.
GENERATOR_STAGES = [
    ("подготовка", "[transcribe/local]"),
    ("транскрипция", " segments, "),
    ("выбор моментов", None),
]
HEARTBEAT_SECONDS = 60


def duration(seconds: float) -> str:
    return str(timedelta(seconds=round(seconds)))


def pump(stream, lines: queue.Queue) -> None:
    for line in stream:
        lines.put(line.rstrip())
    lines.put(None)


def run_generator(cmd: list[str], env: dict) -> "dict[str, float] | None":
    """Гонит генератор, пересылая его вывод в лог. Время этапов или None, если генератор упал."""
    proc = subprocess.Popen(cmd, cwd=config.GENERATOR_DIR, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    lines = queue.Queue()
    threading.Thread(target=pump, args=(proc.stdout, lines), daemon=True).start()
    timings, stage, started = {}, 0, clock.monotonic()
    limit = config.HIGHLIGHTS_TIMEOUT.total_seconds()
    while True:
        name, end_marker = GENERATOR_STAGES[stage]
        if name == "выбор моментов" and clock.monotonic() - started > limit:
            proc.kill()
            proc.wait()
            log.error("Выбор моментов идёт дольше %s, генератор остановлен. Скорее всего, Gemini не "
                      "отвечает или не применён патч генератора", duration(limit))
            return None
        try:
            line = lines.get(timeout=HEARTBEAT_SECONDS)
        except queue.Empty:
            log.info("... %s идёт %s", name, duration(clock.monotonic() - started))
            continue
        if line is None:
            break
        log.info("  %s", line)
        # Длина видео приходит в строке конца транскрипции: длинному видео больше кусков для Gemini.
        audio = re.search(r" segments, (\d+)s of audio", line)
        if audio:
            limit += int(audio[1]) / 3600 * config.HIGHLIGHTS_TIMEOUT_PER_HOUR.total_seconds()
        if end_marker and end_marker in line:
            timings[name] = clock.monotonic() - started
            log.info("Этап «%s»: %s", name, duration(timings[name]))
            stage, started = stage + 1, clock.monotonic()
    timings[name] = clock.monotonic() - started
    rc = proc.wait()
    if rc != 0:
        log.error("Генератор завершился с кодом %d", rc)
        return None
    return timings


def clip_count(seconds: float) -> int:
    """Сколько клипов брать с исходника такой длины."""
    return max(config.CLIPS_MIN, min(config.CLIPS_MAX, round(seconds / 60 / config.CLIP_EVERY_MINUTES)))


def make_clips(source: str, out_dir: Path, layout: str,
               cam: "tuple | None") -> "list[tuple[Path, str]] | None":
    """Генератор выбирает моменты, ffmpeg монтирует клипы. Список (клип, заголовок) или None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Генератору отдаём путь относительно его cwd: так он остаётся ASCII,
    # даже если в пути к проекту есть кириллица.
    out_rel = os.path.relpath(out_dir, config.GENERATOR_DIR)
    result_json = os.path.join(out_rel, "result.json")

    cmd = [str(config.GENERATOR_PYTHON), "main.py", source, "--mode", "local",
           # Длина видео станет известна только после транскрипции, поэтому просим максимум
           # и обрезаем по clip_count: shorts у генератора уже отсортированы по score.
           "--num-clips", str(config.CLIPS_MAX), "--output-json", result_json]
    if config.LANGUAGE:
        cmd += ["--language", config.LANGUAGE]
    log.info("Генератор: %s", out_dir)
    # Без PYTHONUTF8 вывод генератора в pipe идёт в cp1251, и print заголовка с эмодзи роняет его.
    # LOCAL_SKIP_CROP понимает только патч генератора: без него он ещё и кадрирует сам.
    timings = run_generator(cmd, {**os.environ, "LOCAL_OUTPUT_DIR": out_rel, "LOCAL_SKIP_CROP": "true",
                                  "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    if timings is None:
        return None

    result = json.loads((config.GENERATOR_DIR / result_json).read_text(encoding="utf-8"))
    seconds = float(result["transcript"]["duration"])
    shorts = result["shorts"][:clip_count(seconds)]
    log.info("Исходник %s: клипов %d из %d найденных", duration(seconds), len(shorts), len(result["shorts"]))
    video = (config.GENERATOR_DIR / result["source_video_url"]).resolve()
    # Генератор кэширует транскрипт исходника как <имя исходника>.srt в папке вывода.
    srt = out_dir / (video.stem + ".srt")
    cues = read_srt(srt) if srt.is_file() else []
    if not cues:
        log.warning("Нет %s: клипы будут без субтитров и без подгонки к репликам", srt.name)

    spans = [snap(cues, float(s["start_time"]), float(s["end_time"])) for s in shorts]
    faces = None
    if layout == "auto":
        started = clock.monotonic()
        faces = detect_faces(video, spans)
        timings["поиск лиц"] = clock.monotonic() - started

    clips = []
    started = clock.monotonic()
    for i, (short, (start, end)) in enumerate(zip(shorts, spans), 1):
        clip_layout, params = layout, dict(zip("xywh", cam or ()))
        if layout == "auto":
            clip_layout, params, why = (pick_layout(faces["clips"][i - 1], faces["size"]) if faces
                                        else ("fit", {}, "детектор лиц не сработал"))
            log.info("Клип %d: раскладка %s, %s", i, clip_layout, why)
        clip = out_dir / f"short_{i:02d}.mp4"
        ass = None
        if config.SUBTITLES or config.HOOK_TITLE:
            ass = clip.with_suffix(".ass")
            write_ass(ass, clip_cues(cues, start, end) if config.SUBTITLES else [],
                      short["title"] if config.HOOK_TITLE else "", clip_layout, end - start)
        log.info("Монтаж %d/%d: %s, с %.1f по %.1f с", i, len(shorts), clip.name, start, end)
        try:
            render(video, start, end, clip, clip_layout, params, ass)
        except subprocess.CalledProcessError as e:
            log.error("Пропуск, ffmpeg завершился с кодом %d: %s", e.returncode, clip.name)
            continue
        finally:
            if ass:
                ass.unlink(missing_ok=True)
        clips.append((clip, short["title"]))
    timings["монтаж"] = clock.monotonic() - started
    log.info("Готово клипов: %d из %d", len(clips), len(shorts))
    log.info("Время: %s, всего %s", ", ".join(f"{k} {duration(v)}" for k, v in timings.items()),
             duration(sum(timings.values())))
    return clips


def cmd_process(args: argparse.Namespace) -> int:
    resolved = resolve_source(args.source)
    if resolved is None:
        return 1
    source, is_file = resolved

    db = connect()
    if db.execute("SELECT 1 FROM clips WHERE source = ?", (source,)).fetchone():
        log.info("Уже обработан, клипы в очереди: %s", source)
        return 0

    clips = make_clips(source, config.CLIPS_DIR / source_id(source, is_file),
                       args.layout, config.CAM_PRESETS.get(args.cam))
    if clips is None:
        return 1
    db.executemany("INSERT INTO clips (path, caption, source) VALUES (?, ?, ?)",
                   [(str(path), title, source) for path, title in clips])
    db.commit()
    log.info("В очередь добавлено клипов: %d", len(clips))
    return 0


def ask_layout() -> "tuple[str, str] | None":
    """Спрашивает макет и пресет вебки. Вопросы здесь, а не в cut.bat: cmd ломает UTF-8 bat с кириллицей."""
    layout = {"2": "fit", "3": "split", "4": "crop"}.get(input(
        "Макет: 1 auto (по лицам в кадре), 2 fit (весь кадр на размытом фоне), "
        "3 split (вебка сверху, экран снизу), 4 crop (обрезка по центру)\nНомер [1]: ").strip(), "auto")
    if layout != "split":
        return layout, None
    presets = list(config.CAM_PRESETS)
    cam = input(f"Пресет вебки ({', '.join(presets)}) [{presets[0]}]: ").strip() or presets[0]
    if cam not in config.CAM_PRESETS:
        log.error("Нет пресета %s, есть: %s", cam, ", ".join(presets))
        return None
    return layout, cam


def cmd_cut(args: argparse.Namespace) -> int:
    raw = args.source or input("Ссылка или путь к видео: ").strip().strip('"')
    resolved = resolve_source(raw)
    if resolved is None:
        return 1
    source, is_file = resolved
    if args.ask:
        answer = ask_layout()
        if answer is None:
            return 1
        args.layout, args.cam = answer

    out_dir = config.PREVIEW_DIR / source_id(source, is_file)
    if make_clips(source, out_dir, args.layout, config.CAM_PRESETS.get(args.cam)) is None:
        return 1
    if os.name == "nt":
        os.startfile(out_dir)
    return 0


# -- upload ---------------------------------------------------------------------


def taken_slots(db) -> set:
    rows = db.execute("SELECT slot FROM clips WHERE status IN ('uploaded', 'unknown') AND slot IS NOT NULL")
    return {datetime.fromisoformat(r["slot"]) for r in rows}


def next_free_slot(taken: set) -> "datetime | None":
    now = datetime.now(config.TZ)
    earliest, latest = now + config.MIN_LEAD, now + config.MAX_LEAD
    for day in range(config.MAX_LEAD.days + 2):
        date = now.date() + timedelta(days=day)
        for hhmm in sorted(config.SLOTS):
            slot = datetime.combine(date, time.fromisoformat(hhmm), config.TZ)
            if earliest <= slot <= latest and slot not in taken:
                return slot
    return None


def cmd_upload(args) -> int:
    db = connect()
    pending = db.execute("SELECT * FROM clips WHERE status = 'pending' ORDER BY id").fetchall()
    if not pending:
        print("Очередь пуста.")
        return 0
    taken = taken_slots(db)
    for clip in pending:
        slot = next_free_slot(taken)
        if slot is None:
            print(f"Свободных слотов в ближайшие {config.MAX_LEAD.days} дней нет, "
                  f"в очереди осталось: {len(pending) - pending.index(clip)}")
            break
        if not Path(clip["path"]).is_file():
            db.execute("UPDATE clips SET status = 'failed', error = ? WHERE id = ?",
                       ("файл клипа не найден", clip["id"]))
            db.commit()
            continue

        # До вызова помечаем unknown: если нас прервут посреди заливки,
        # клип не уйдёт повторно сам.
        db.execute("UPDATE clips SET status = 'unknown', slot = ?, error = ? WHERE id = ?",
                   (slot.isoformat(), "заливка прервана", clip["id"]))
        db.commit()

        delay = int((slot - datetime.now(config.TZ)).total_seconds())
        print(f"[{clip['id']}] {Path(clip['path']).name} → {slot:%Y-%m-%d %H:%M}", flush=True)
        proc = subprocess.run(
            [str(config.UPLOADER_PYTHON), "cli.py", "upload", "-u", config.TIKTOK_ACCOUNT,
             "-v", clip["path"], "-t", clip["caption"], "-sc", str(delay)],
            cwd=config.UPLOADER_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUTF8": "1"},
        )
        output = (proc.stdout + proc.stderr).strip()
        print(output)

        if proc.returncode == 0:
            status, error = "uploaded", None
        elif UNCERTAIN_MARKER in output.lower() or "Traceback" in output:
            status, error = "unknown", output[-1000:]
        else:
            status, error = "failed", output[-1000:]
        db.execute("UPDATE clips SET status = ?, error = ? WHERE id = ?", (status, error, clip["id"]))
        db.commit()
        if status != "failed":
            taken.add(slot)
    return 0


# -- status / retry -------------------------------------------------------------


def cmd_status(args) -> int:
    rows = connect().execute("SELECT * FROM clips ORDER BY id").fetchall()
    if not rows:
        print("Очередь пуста.")
        return 0
    for r in rows:
        slot = datetime.fromisoformat(r["slot"]).strftime("%Y-%m-%d %H:%M") if r["slot"] else "-"
        print(f"{r['id']:>4}  {r['status']:<8}  {slot:<16}  {r['caption'][:50]}")
        if r["status"] != "uploaded":
            print(f"      {r['path']}")
        if r["error"]:
            print(f"      ! {r['error'].splitlines()[-1][:200]}")
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return 0


def cmd_retry(args) -> int:
    db = connect()
    for clip_id in args.ids:
        row = db.execute("SELECT status FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None:
            print(f"{clip_id}: нет такого клипа")
        elif row["status"] not in ("failed", "unknown"):
            print(f"{clip_id}: статус {row['status']}, возвращать нечего")
        else:
            db.execute("UPDATE clips SET status = 'pending', slot = NULL, error = NULL WHERE id = ?",
                       (clip_id,))
            print(f"{clip_id}: {row['status']} → pending")
    db.commit()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("process", help="нарезать, смонтировать, добавить клипы в очередь")
    p.add_argument("source", help="путь к видео или ссылка")
    p.set_defaults(func=cmd_process)
    c = sub.add_parser("cut", help="только нарезать в data/preview, без очереди")
    c.add_argument("source", nargs="?", help="путь к видео или ссылка; без него спросит")
    c.add_argument("--ask", action="store_true", help="спросить макет и пресет вебки (для cut.bat)")
    c.set_defaults(func=cmd_cut)
    for s in (p, c):
        s.add_argument("--layout", choices=["auto", "fit", "split", "crop"], default=config.LAYOUT,
                       help=f"макет клипа (по умолчанию {config.LAYOUT})")
        s.add_argument("--cam", choices=list(config.CAM_PRESETS),
                       help="пресет вебки для split; при auto включает split с этим пресетом")
    sub.add_parser("upload", help="залить pending-клипы в свободные слоты").set_defaults(func=cmd_upload)
    sub.add_parser("status", help="показать очередь").set_defaults(func=cmd_status)
    r = sub.add_parser("retry", help="вернуть failed/unknown в pending")
    r.add_argument("ids", nargs="+", type=int)
    r.set_defaults(func=cmd_retry)
    args = parser.parse_args()
    if getattr(args, "layout", None) == "auto" and args.cam:
        args.layout = "split"
    if getattr(args, "layout", None) == "split" and not args.cam:
        parser.error(f"для --layout split нужен --cam, пресеты: {', '.join(config.CAM_PRESETS)}")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
