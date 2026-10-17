"""Оркестратор: python -m app {process,upload,status,retry}."""
import argparse
import hashlib
import json
import logging
import math
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time as clock
from concurrent.futures import ThreadPoolExecutor
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
# fit: картинка 16:9 по центру по вертикали, заголовок прямо над ней. Две строки заголовка
# над y=656 начинаются около y=420, то есть внутри безопасной зоны.
FIT_TOP = (1920 - 1080 * 9 // 16) // 2      # 656
SPLIT_CAM_H = 768                          # в split вебка 1080x768 сверху, экран 1080x1152 снизу


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


def write_ass(path: Path, cues: list[Cue], title: str, pos: tuple[str, str], duration: float) -> None:
    """pos: теги позиции субтитров и заголовка."""
    lines = [f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Sub,,0,0,0,,{pos[0]}{ass_text(text)}"
             for a, b, text in cues]
    if title:
        shown = config.TITLE_SECONDS or duration
        text, size = title_layout(title)
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(shown)},Title,,0,0,0,,"
                     f"{pos[1]}{{\\fs{size}}}{text}")
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
           "boxblur=10:2,scale=1080:1920[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
    "split": "[0:v]split[a][b];[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
             f"scale=1080:{SPLIT_CAM_H}:force_original_aspect_ratio=increase,crop=1080:{SPLIT_CAM_H}[cam];"
             # Экран чуть приближен: при полной высоте в кадр попадал край вебки.
             f"[b]crop=ih*0.9*1080/{1920 - SPLIT_CAM_H}:ih*0.9,scale=1080:{1920 - SPLIT_CAM_H}[scr];"
             "[cam][scr]vstack",
    # Обрезка по центру. В режиме crop остаётся на случай, если детектор лиц упал.
    "crop": "[0:v]scale=-2:1920,crop=1080:1920",
    # Окно 9:16 во всю высоту, центр по x следует за лицом: {cx} это выражение от t. Этим же
    # фильтром монтируется режим crop.
    "face": "[0:v]crop=w=ih*9/16:h=ih:x='clip(({cx})*iw-ow/2,0,iw-ow)':y=0,scale=1080:1920",
}
# Теги позиции. fit: заголовок низом прямо над картинкой, субтитры под ней. split: заголовок под
# вебкой, субтитры под заголовком, выше прицела (центр экрана, y=1344) и интерфейса TikTok внизу.
# crop и face: заголовок сверху, субтитры снизу, оба внутри безопасной зоны.
def fit_pos(top: int) -> tuple[str, str]:
    """Позиции fit, когда верх картинки на top: субтитры под картинкой, заголовок низом над ней."""
    return rf"{{\an8\pos({SAFE_CX},{1920 - top + 20})}}", rf"{{\an2\pos(540,{top - 24})}}"


TOP_TITLE = rf"{{\an8\pos(540,{TEXT_TOP})}}"
BOTTOM_SUB = rf"{{\an2\pos({SAFE_CX},{TEXT_BOTTOM})}}"
SUB_POS = {"fit": fit_pos(FIT_TOP)[0], "split": rf"{{\an8\pos({SAFE_CX},{SPLIT_CAM_H + 250})}}",
           "crop": BOTTOM_SUB, "face": BOTTOM_SUB}
TITLE_POS = {"fit": fit_pos(FIT_TOP)[1], "split": rf"{{\an8\pos(540,{SPLIT_CAM_H + 22})}}",
             "crop": TOP_TITLE, "face": TOP_TITLE}


def cam_rect(face: list[float], size: list[float]) -> dict:
    """Рамка вебки вокруг лица: высота AUTO_CAM_SCALE лиц, пропорции верхней панели split."""
    h = min(1.0, face[3] * config.AUTO_CAM_SCALE)
    w = min(1.0, h * size[1] / size[0] * 1080 / SPLIT_CAM_H)
    x = min(max(face[0] - w / 2, 0.0), 1 - w)
    y = min(max(face[1] - h * config.AUTO_CAM_FACE_Y, 0.0), 1 - h)
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def pick_layout(samples: list, size: list[float]) -> tuple[str, dict, str]:
    """(раскладка, поля фильтра, причина для лога) по замерам лиц одного клипа. Выражение слежения
    для face подставляет потом track_faces."""
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
        return "face", {}, f"крупное лицо (высота {h:.2f}, x {cx:.2f}) в {share:.0%} замеров"
    # IRL в движении: человек ходит, медиана размера и места ничего не говорит, поэтому
    # считаем замеры, где лицо заметное и не у края.
    walking = sum(f[3] >= config.AUTO_SMALL_FACE and abs(f[0] - 0.5) < config.AUTO_EDGE for f in biggest) / n
    if walking >= config.AUTO_IRL_SHARE:
        return "face", {}, f"лицо двигается, но в {walking:.0%} замеров заметное и не у края"
    return "fit", {}, (f"крупнейшее лицо в {share:.0%} замеров, высота {h:.2f}, x {cx:.2f}, "
                       f"заметное и не у края только в {walking:.0%}: не вебка и не IRL")


# Граница сцены чуть раньше склейки: время кадра в выражении и в выводе select может
# разойтись в третьем знаке, а соседние кадры отстоят друг от друга хотя бы на 1/60 с.
CUT_EPS = 0.005


def scenes(cuts: list[float], length: float) -> list[tuple[float, float]]:
    """Сцены клипа по склейкам. Сцена короче CROP_SCENE_MIN присоединяется к предыдущей,
    а если она первая, к следующей."""
    starts = [0.0]
    for cut in cuts:
        if cut - starts[-1] >= config.CROP_SCENE_MIN:
            starts.append(cut - CUT_EPS)
    if len(starts) > 1 and length - starts[-1] < config.CROP_SCENE_MIN:
        starts.pop()
    return list(zip(starts, starts[1:] + [length]))


def same_face(boxes: list, face: list[float]) -> "list[float] | None":
    """Лицо из boxes ближе всех к face, если оно сдвинулось не дальше своего размера."""
    near = min(boxes, key=lambda f: abs(f[0] - face[0]) + abs(f[1] - face[1]))
    return near if abs(near[0] - face[0]) <= face[2] and abs(near[1] - face[1]) <= face[3] else None


def scene_targets(samples: list, window: float) -> "list[float | None]":
    """Центр окна по x в каждом замере сцены: на главном лице или между ним и вторым, если оба
    влезают в окно. None, если главного лица в замере нет."""
    need = round(config.CROP_SWITCH_SECONDS * config.CROP_SAMPLE_FPS)
    main, rivals, out = None, 0, []
    for _, boxes in samples:
        cur = same_face(boxes, main) if boxes and main else None
        biggest = max(boxes, key=lambda f: f[3]) if boxes else None
        # Гистерезис: другое лицо становится главным, только если оно заметно крупнее (или главного
        # не видно) несколько замеров подряд. Иначе кадр мечется между собеседниками.
        rivals = rivals + 1 if biggest and (cur is None or biggest[3] > cur[3] * config.CROP_SWITCH) else 0
        if biggest and (main is None or rivals >= need):
            cur, rivals = biggest, 0
        if cur is None:
            out.append(None)
            continue
        main = cur
        left, right = cur[0] - cur[2] / 2, cur[0] + cur[2] / 2
        pair = [f for f in boxes if f is not cur and f[3] >= cur[3] * config.CROP_PAIR]
        if pair:
            other = max(pair, key=lambda f: f[3])
            both = min(left, other[0] - other[2] / 2), max(right, other[0] + other[2] / 2)
            if both[1] - both[0] <= window:
                left, right = both
        out.append((left + right) / 2)
    return out


def fill_gaps(ts: list[float], xs: "list[float | None]") -> list[float]:
    """Замеры без лица: до CROP_LOST_HOLD держим прошлое место (в начале сцены место, где лицо
    скоро найдётся), дольше центр."""
    first = next(((t, x) for t, x in zip(ts, xs) if x is not None), None)
    out, last = [], None
    for t, x in zip(ts, xs):
        if x is not None:
            last = (t, x)
        elif last and t - last[0] <= config.CROP_LOST_HOLD:
            x = last[1]
        elif not last and first and first[0] - t <= config.CROP_LOST_HOLD:
            x = first[1]
        out.append(0.5 if x is None else x)
    return out


# Шаг расчёта движения окна, с. На шаге замеров (1/3 с) разгон выходил ступенькой: первый же
# отрезок между точками уже шёл почти с полной скоростью.
FOLLOW_STEP = 0.1


def follow(ts: list[float], xs: list[float], end: float, window: float) -> list[tuple[float, float]]:
    """Точки (t, x) пути окна от первого замера до end: окно стоит, пока цель в мёртвой зоне, иначе
    едет к ней. Два сглаживания подряд дают плавный разгон и торможение без перелёта."""
    goal = a = c = xs[0]
    out = [(ts[0], c)]
    for t, x, nxt in zip(ts, xs, ts[1:] + [end]):
        if abs(x - goal) > config.CROP_DEADZONE * window:
            goal = x
        n = max(1, math.ceil((nxt - t) / FOLLOW_STEP - 1e-9))
        k = 1 - math.exp(-(nxt - t) / n / config.CROP_SMOOTH)
        for j in range(1, n + 1):
            a += k * (goal - a)
            c += k * (a - c)
            out.append((t + (nxt - t) * j / n, c))
    return out


# Допуск упрощения пути окна в долях ширины кадра: около пикселя на 1920.
KNOT_TOL = 0.0005


def on_line(points: list[tuple[float, float]]) -> bool:
    """Внутренние точки лежат на отрезке между крайними с допуском KNOT_TOL. Скачок не отрезок."""
    (ta, xa), (tb, xb) = points[0], points[-1]
    if tb - ta < 0.001:
        return False
    return all(abs(xa + (xb - xa) * (t - ta) / (tb - ta) - x) <= KNOT_TOL for t, x in points[1:-1])


def bisect_expr(segs: list[tuple[float, str]]) -> str:
    """if по времени двоичным поиском: отрезок segs[i] действует с segs[i][0] до начала следующего.
    eval в ffmpeg не разбирает цепочку глубже ~90 узлов, ни вложенных if подряд, ни длинной суммы,
    а у дерева глубина log2 от числа отрезков."""
    if len(segs) == 1:
        return segs[0][1]
    mid = len(segs) // 2
    return f"if(lt(t,{segs[mid][0]}),{bisect_expr(segs[:mid])},{bisect_expr(segs[mid:])})"


def knots_expr(knots: list[tuple[float, float]]) -> str:
    """Кусочно-линейное выражение ffmpeg от t по точкам (t, x). Две точки в одно время дают скачок."""
    # Точки, через которые ломаная и так проходит, выкидываем: выражение короче.
    keep, i = [knots[0]], 0
    while i < len(knots) - 1:
        j = i + 1
        while j + 1 < len(knots) and on_line(knots[i:j + 2]):
            j += 1
        keep.append(knots[j])
        i = j
    keep = [(round(t, 3), round(x, 4)) for t, x in keep]
    segs = [(t0, f"{x0}" if x0 == x1 else f"{x0}+{(x1 - x0) / (t1 - t0):.5f}*(t-{t0})")
            for (t0, x0), (t1, x1) in zip(keep, keep[1:]) if t1 - t0 >= 0.001]
    return bisect_expr(segs + [(keep[-1][0], f"{keep[-1][1]}")])


def crop_track(samples: list, cuts: list[float], size: list[float], length: float) -> tuple[str, int, int]:
    """Выражение ffmpeg от t для центра окна 9:16 по x в долях кадра, число сцен и сколько из них
    со слежением. На склейке окно сразу встаёт на место, внутри сцены едет плавно."""
    window = min(1.0, size[1] * 9 / 16 / size[0])
    # Короткий кусок, присоединённый к сцене, это другой план: его лица окно не двигают.
    bounds = [0.0, *(cut - CUT_EPS for cut in cuts), length]
    short = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a < config.CROP_SCENE_MIN]
    samples = [(t, [] if any(a <= t < b for a, b in short) else boxes) for t, boxes in samples]
    knots, tracked = [], 0
    parts = scenes(cuts, length)
    for start, end in parts:
        part = [(t, boxes) for t, boxes in samples if start <= t < end]
        targets = scene_targets(part, window)
        found = sum(x is not None for x in targets)
        if not found or found < len(part) * config.CROP_FACE_SHARE:
            knots += [(start, 0.5), (end, 0.5)]
            continue
        tracked += 1
        ts = [t for t, _ in part]
        xs = [min(max(x, window / 2), 1 - window / 2) for x in fill_gaps(ts, targets)]
        path = follow(ts, xs, end, window)
        knots += [(start, path[0][1]), *path]
    return knots_expr(knots), len(parts), tracked


def bar_rows(video: Path, t: float) -> "tuple[int, int, int, int] | None":
    """(ширина, высота, строк полосы сверху, строк снизу) кадра в момент t или None. Строка полосы та,
    где ни один пиксель не ярче BARS_BLACK. cropdetect смотрит среднюю яркость строки, и у него за
    полосу сходит тёмный край комнаты или ночной игры. Берётся первый ключевой кадр после t в один
    поток: точная перемотка на 1080p60 декодировала до секунды на кадр, а параллельные декодеры дрались
    за ядра. После последнего ключевого кадра файла такого нет, тогда обычная перемотка."""
    for fast in (["-skip_frame", "nokey"], []):
        proc = subprocess.run([config.FFMPEG, "-v", "error", *fast, "-threads", "1", "-ss", f"{t:.3f}",
                               "-i", str(video), "-frames:v", "1", "-vf", "format=gray", "-c:v", "pgm",
                               "-f", "image2pipe", "-"], capture_output=True)
        if proc.returncode != 0 or proc.stdout:
            break
    if proc.returncode != 0 or not proc.stdout:
        log.error("ffmpeg не отдал кадр %.1f с для поиска полос:\n%s", t,
                  proc.stderr.decode(errors="replace").strip()[-2000:])
        return None
    # PGM от ffmpeg: "P5\n<ширина> <высота>\n255\n" и байты яркости построчно.
    _, dims, _, pixels = proc.stdout.split(b"\n", 3)
    w, h = map(int, dims.split())
    def black(y: int) -> bool:
        return max(pixels[y * w:(y + 1) * w]) <= config.BARS_BLACK

    top = next((y for y in range(h) if not black(y)), h)
    bottom = next((h - 1 - y for y in range(h - 1, -1, -1) if not black(y)), h)
    return w, h, top, bottom


def detect_bars(video: Path, start: float, end: float) -> "str | None":
    """Рамка без чёрных полос сверху и снизу w:h:0:y по BARS_POINTS кадрам клипа или None. Полосы
    срезаются, только если почти во всех кадрах они одной высоты и сверху примерно как снизу:
    тёмная картинка так стабильно себя не ведёт."""
    points = [start + (k + 0.5) * (end - start) / config.BARS_POINTS for k in range(config.BARS_POINTS)]
    # Кадры параллельно: почти всё время каждого уходит на перемотку от ключевого кадра.
    with ThreadPoolExecutor() as pool:
        frames = list(pool.map(lambda t: bar_rows(video, t), points))
    if None in frames:
        return None
    w, h = frames[0][:2]
    tol = config.BARS_TOL * h
    pictures = [f for f in frames if f[2] + f[3] < h]  # совсем чёрный кадр ничего не говорит
    if not pictures:
        return None
    top, bottom = median(f[2] for f in pictures), median(f[3] for f in pictures)
    agree = [f for f in pictures if abs(f[2] - top) <= tol and abs(f[3] - bottom) <= tol]
    if (len(agree) < config.BARS_AGREE * len(frames) or min(top, bottom) < config.BARS_MIN * h
            or abs(top - bottom) > tol):
        return None
    # Срезаем по самой узкой полосе из согласных кадров, чтобы не задеть картинку.
    y = (min(f[2] for f in agree) + 1) // 2 * 2
    return f"{w}:{(h - y - min(f[3] for f in agree)) // 2 * 2}:0:{y}"


def frame_size(bars: "str | None", size: list[float]) -> list[float]:
    """Размер кадра после обрезки полос."""
    return [int(v) for v in bars.split(":")[:2]] if bars else size


def detect_faces(video: Path, spans: list[tuple[float, float]], step: float, **extra) -> "dict | None":
    """Замеры лиц по клипам из app/faces.py или None, если детектор упал. extra уходит в его опции."""
    opts = {"spans": spans, "step": step, "score": config.AUTO_FACE_SCORE, "ffmpeg": config.FFMPEG, **extra}
    proc = subprocess.run([str(config.GENERATOR_PYTHON), str(Path(__file__).with_name("faces.py")),
                           str(config.FACE_MODEL), str(video), json.dumps(opts)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log.error("Детектор лиц завершился с кодом %d:\n%s", proc.returncode, proc.stderr.strip()[-2000:])
        return None
    return json.loads(proc.stdout)


def track_faces(video: Path, spans: list[tuple[float, float]], bars: list, plans: list) -> list:
    """Подставляет окно 9:16 по лицам в планы (раскладка, поля, причина) с раскладкой face (auto)
    или crop. Замеры отдельные, 3 в секунду со склейками: замеры auto раз в секунду для выбора
    раскладки остаются как были. Если детектор упал, face уходит в fit, crop режется по центру."""
    ids = [i for i, (name, _, _) in enumerate(plans) if name in ("face", "crop")]
    track = detect_faces(video, [spans[i] for i in ids], 1 / config.CROP_SAMPLE_FPS,
                         crops=[bars[i] for i in ids], scene=config.CROP_SCENE)
    plans = list(plans)
    for k, i in enumerate(ids):
        name, _, why = plans[i]
        if track is None:
            plans[i] = (("fit", {}, "слежение не сработало") if name == "face" else
                        ("crop", {}, "детектор лиц не сработал, обрезка по центру"))
            continue
        start, end = spans[i]
        cx, total, tracked = crop_track(track["clips"][k], track["cuts"][k],
                                        frame_size(bars[i], track["size"]), end - start)
        plans[i] = ("face", {"cx": cx}, ", ".join(
            filter(None, [why, f"сцен {total}, со слежением {tracked}, по центру {total - tracked}"])))
    return plans


def render(video: Path, start: float, end: float, dst: Path, layout: str,
           params: dict, ass: "Path | None", bars: "str | None") -> None:
    # fps первым фильтром: дальше вся цепочка, включая ass, обрабатывает не больше OUTPUT_FPS кадров в секунду.
    # Сразу за ним срезаются чёрные полосы, и макет получает чистую картинку.
    pre = f"fps='min(source_fps,{config.OUTPUT_FPS})'," + (f"crop={bars}," if bars else "")
    graph = LAYOUTS[layout].format(**params).replace("[0:v]", f"[0:v]{pre}", 1) + ",setsar=1"
    if ass:
        # Имя без пути, cwd в папке клипа, fontsdir относительный: в фильтрах ffmpeg
        # двоеточие из C:\ надо экранировать.
        fonts = Path(os.path.relpath(config.FONTS_DIR, dst.parent)).as_posix()
        graph += f",ass={ass.name}:fontsdir={fonts}"
    # Граф файлом (-/ читает значение опции из файла): выражение слежения в crop бывает длиннее
    # лимита командной строки Windows в 32767 символов.
    script = dst.with_suffix(".filter")
    script.write_text(graph + "[v]", encoding="utf-8")
    cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", str(video), "-/filter_complex", script.name, "-map", "[v]", "-map", "0:a?",
           "-af", f"loudnorm=I={config.LOUDNESS}:TP=-1.5:LRA=11", "-ar", "48000",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    try:
        subprocess.run(cmd, check=True, cwd=dst.parent)
    finally:
        script.unlink(missing_ok=True)


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


def clips_min(minutes: float, kind: str) -> int:
    if kind == "film":
        return max(config.FILM_CLIPS_MIN, round(minutes / config.FILM_MINUTES_PER_CLIP))
    return next(n for limit, n in config.CLIPS_MIN if minutes < limit)


def pick_moments(highlights: list[dict], minutes: float, kind: str) -> list[dict]:
    """Моменты со score от CLIP_MIN_SCORE по убыванию score, не меньше минимума для длины исходника
    и не больше CLIPS_MAX."""
    ranked = sorted(highlights, key=lambda h: int(h.get("score", 0)), reverse=True)
    good = sum(int(h.get("score", 0)) >= config.CLIP_MIN_SCORE for h in ranked)
    return ranked[:min(config.CLIPS_MAX[kind], max(clips_min(minutes, kind), good))]


GEMINI_BLOCKED = 3  # код выхода app/gemini.py: промпт заблокирован фильтром


def ask_gemini(prompt: str) -> "tuple[bool, dict | list | None]":
    """(промпт заблокирован фильтром, JSON-ответ Gemini через app/gemini.py или None при ошибке)."""
    proc = subprocess.run([str(config.GENERATOR_PYTHON), str(Path(__file__).with_name("gemini.py"))],
                          input=prompt, cwd=config.GENERATOR_DIR, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode == GEMINI_BLOCKED:
        return True, None
    if proc.returncode != 0:
        log.error("Запрос к Gemini завершился с кодом %d:\n%s", proc.returncode, proc.stderr.strip()[-2000:])
        return False, None
    try:
        return False, json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        log.error("Gemini вернул не JSON (%s):\n%s", e, proc.stdout[-2000:])
        return False, None


TITLES_FORMAT = """
Ответь только JSON без пояснений:
{"titles":[{"clip":1,"kind":"string","title":"string"}]}
Заголовок нужен для каждого клипа, clip это его номер."""


def rewrite_titles(texts: list[str], titles: list[str], kind: str, streamer: str,
                   name: str) -> "list[tuple[str, str] | None] | None":
    """Один запрос к Gemini на все клипы: (вид момента, новый заголовок) по транскрипту клипа, None на
    месте клипа, который фильтр Gemini не пропускает, или None целиком при ошибке."""
    who = ""
    if kind == "stream":
        who = (f"Ник стримера: {streamer}. Пиши его ровно так, буква в букву.\n" if streamer else
               f"Ник стримера не задан. Возьми его из названия видео или ссылки: {name}\n")
    clips = "\n\n".join(f"Клип {i}. Черновой заголовок: {title}\nТранскрипт: {text}"
                        for i, (text, title) in enumerate(zip(texts, titles), 1))
    prompt = f"{config.TITLES_PROMPT[kind].read_text(encoding='utf-8')}\n{who}{TITLES_FORMAT}\n\n{clips}"
    blocked, answer = ask_gemini(prompt)
    if blocked and len(texts) == 1:
        log.warning("Фильтр Gemini не пропускает клип, он пропущен: %s", titles[0])
        return [None]
    if blocked:
        # Фильтр блокирует и сочетание клипов, каждый из которых по отдельности проходит:
        # на IRL ЭВЕЛОНА так было с 10 клипами.
        log.warning("Фильтр Gemini заблокировал запрос на %d клипов, делю пополам", len(texts))
        mid = len(texts) // 2
        head = rewrite_titles(texts[:mid], titles[:mid], kind, streamer, name)
        tail = head and rewrite_titles(texts[mid:], titles[mid:], kind, streamer, name)
        return head + tail if tail else None
    if answer is None:
        return None
    # Модель иногда отдаёт голый список вместо {"titles": [...]}.
    items = answer if isinstance(answer, list) else answer.get("titles", [])
    got = {int(t["clip"]): (str(t.get("kind", "")), str(t["title"]).strip())
           for t in items if t.get("clip") and t.get("title")}
    missing = [i for i in range(1, len(texts) + 1) if not got.get(i, ("", ""))[1]]
    if missing:
        log.error("Gemini не дал заголовки клипам %s:\n%s", missing, json.dumps(answer, ensure_ascii=False))
        return None
    return [got[i] for i in range(1, len(texts) + 1)]


def make_clips(source: str, out_dir: Path, layout: str, kind: str,
               streamer: str) -> "list[tuple[Path, str]] | None":
    """Генератор выбирает моменты, ffmpeg монтирует клипы. Список (клип, заголовок) или None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Генератору отдаём путь относительно его cwd: так он остаётся ASCII,
    # даже если в пути к проекту есть кириллица.
    out_rel = os.path.relpath(out_dir, config.GENERATOR_DIR)
    result_json = os.path.join(out_rel, "result.json")

    cmd = [str(config.GENERATOR_PYTHON), "main.py", source, "--mode", "local",
           # Клипы берём из всех кандидатов (highlights), а не из его shorts. Большое --num-clips
           # нужно только затем, чтобы промпт просил у модели больше кандидатов.
           "--num-clips", str(config.CLIPS_MAX[kind]), "--output-json", result_json]
    if config.LANGUAGE:
        cmd += ["--language", config.LANGUAGE]
    log.info("Генератор: %s", out_dir)
    # Без PYTHONUTF8 вывод генератора в pipe идёт в cp1251, и print заголовка с эмодзи роняет его.
    # LOCAL_SKIP_CROP понимает только патч генератора: без него он ещё и кадрирует сам.
    timings = run_generator(cmd, {**os.environ, "LOCAL_OUTPUT_DIR": out_rel, "LOCAL_SKIP_CROP": "true",
                                  "HIGHLIGHTS_PROMPT_FILE": str(config.HIGHLIGHTS_PROMPT),
                                  "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    if timings is None:
        return None

    result = json.loads((config.GENERATOR_DIR / result_json).read_text(encoding="utf-8"))
    minutes = float(result["transcript"]["duration"]) / 60
    shorts = pick_moments(result["highlights"], minutes, kind)
    log.info("Кандидатов %d, score: %s. Клипов %d (порог %d, от %d до %d при длине %.0f мин)",
             len(result["highlights"]),
             " ".join(str(h.get("score")) for h in sorted(result["highlights"], key=lambda h: -int(h.get("score", 0)))),
             len(shorts), config.CLIP_MIN_SCORE, clips_min(minutes, kind), config.CLIPS_MAX[kind], minutes)
    video = (config.GENERATOR_DIR / result["source_video_url"]).resolve()
    # Генератор кэширует транскрипт исходника как <имя исходника>.srt в папке вывода.
    srt = out_dir / (video.stem + ".srt")
    cues = read_srt(srt) if srt.is_file() else []
    if not cues:
        log.warning("Нет %s: клипы будут без субтитров и без подгонки к репликам", srt.name)

    spans = [snap(cues, float(s["start_time"]), float(s["end_time"])) for s in shorts]
    started = clock.monotonic()
    texts = [" ".join(text for a, b, text in cues if b > start and a < end) for start, end in spans]
    titles = rewrite_titles(texts, [s["title"] for s in shorts], kind, streamer,
                            Path(source).name if Path(source).is_file() else source)
    if titles is None:
        return None
    kept = [i for i, t in enumerate(titles) if t]
    shorts, spans, titles = [shorts[i] for i in kept], [spans[i] for i in kept], [titles[i] for i in kept]
    for i, (short, (what, title)) in enumerate(zip(shorts, titles), 1):
        log.info("Клип %d, %s: %s (было: %s)", i, what or "без вида", title, short["title"])
        if len(title) > 50:
            log.warning("Клип %d: заголовок длиннее 50 символов", i)
        short["title"] = title
    timings["заголовки"] = clock.monotonic() - started
    started = clock.monotonic()
    bars = [detect_bars(video, start, end) for start, end in spans]
    timings["поиск полос"] = clock.monotonic() - started
    plans = [(layout, {}, "")] * len(spans)
    if layout == "auto":
        started = clock.monotonic()
        faces = detect_faces(video, spans, config.AUTO_SAMPLE_SECONDS, crops=bars)
        plans = ([pick_layout(samples, frame_size(bar, faces["size"])) for samples, bar in zip(faces["clips"], bars)]
                 if faces else [("fit", {}, "детектор лиц не сработал")] * len(spans))
        timings["поиск лиц"] = clock.monotonic() - started
    if any(name in ("face", "crop") for name, _, _ in plans):
        started = clock.monotonic()
        plans = track_faces(video, spans, bars, plans)
        timings["слежение"] = clock.monotonic() - started

    clips = []
    started = clock.monotonic()
    for i, (short, (start, end), (clip_layout, params, why)) in enumerate(zip(shorts, spans, plans), 1):
        if why:
            log.info("Клип %d: раскладка %s, %s", i, clip_layout, why)
        pos = SUB_POS[clip_layout], TITLE_POS[clip_layout]
        if bars[i - 1]:
            log.info("Клип %d: кадр без чёрных полос %s", i, bars[i - 1])
            if clip_layout == "fit":
                w, h = (int(v) for v in bars[i - 1].split(":")[:2])
                pos = fit_pos((1920 - round(540 * h / w) * 2) // 2)  # высота картинки как у scale=1080:-2
        clip = out_dir / f"short_{i:02d}.mp4"
        ass = None
        if config.SUBTITLES or config.HOOK_TITLE:
            ass = clip.with_suffix(".ass")
            write_ass(ass, clip_cues(cues, start, end) if config.SUBTITLES else [],
                      short["title"] if config.HOOK_TITLE else "", pos, end - start)
        log.info("Монтаж %d/%d: %s, с %.1f по %.1f с", i, len(shorts), clip.name, start, end)
        try:
            render(video, start, end, clip, clip_layout, params, ass, bars[i - 1])
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

    clips = make_clips(source, config.CLIPS_DIR / source_id(source, is_file), args.layout, args.type,
                       args.streamer)
    if clips is None:
        return 1
    db.executemany("INSERT INTO clips (path, caption, source) VALUES (?, ?, ?)",
                   [(str(path), title, source) for path, title in clips])
    db.commit()
    log.info("В очередь добавлено клипов: %d", len(clips))
    return 0


MENU = {"1": ("stream", "auto"), "2": ("stream", "crop"), "3": ("film", "fit"), "4": ("film", "crop")}


def ask_mode() -> tuple[str, str, str]:
    """(тип, режим, ник стримера). Вопросы здесь, а не в cut.bat: cmd ломает UTF-8 bat с кириллицей."""
    kind, layout = MENU.get(input("1 Стрим (авто), 2 Стрим IRL (обрезка), 3 Фильм: весь кадр, "
                                  "4 Фильм: обрезка\nНомер [1]: ").strip(), MENU["1"])
    streamer = input("Ник стримера [из названия]: ").strip() if kind == "stream" else ""
    return kind, layout, streamer


def cmd_cut(args: argparse.Namespace) -> int:
    raw = args.source or input("Ссылка или путь к видео: ").strip().strip('"')
    resolved = resolve_source(raw)
    if resolved is None:
        return 1
    source, is_file = resolved
    if args.ask:
        args.type, args.layout, args.streamer = ask_mode()

    out_dir = config.PREVIEW_DIR / source_id(source, is_file)
    if make_clips(source, out_dir, args.layout, args.type, args.streamer) is None:
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
    c.add_argument("--ask", action="store_true", help="спросить тип, режим и ник (для cut.bat)")
    c.set_defaults(func=cmd_cut)
    for s in (p, c):
        s.add_argument("--layout", choices=["auto", "fit", "crop"], default=config.LAYOUT,
                       help=f"auto: раскладка по лицам (стримы), fit: фильм, весь кадр, "
                            f"crop: фильм, обрезка 9:16 по лицам (по умолчанию {config.LAYOUT})")
        s.add_argument("--type", choices=["stream", "film"], default="stream",
                       help="стиль заголовков: стрим или фильм (по умолчанию stream)")
        s.add_argument("--streamer", default="",
                       help="ник стримера для заголовков; без него модель возьмёт ник из названия")
    sub.add_parser("upload", help="залить pending-клипы в свободные слоты").set_defaults(func=cmd_upload)
    sub.add_parser("status", help="показать очередь").set_defaults(func=cmd_status)
    r = sub.add_parser("retry", help="вернуть failed/unknown в pending")
    r.add_argument("ids", nargs="+", type=int)
    r.set_defaults(func=cmd_retry)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
