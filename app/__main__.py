"""Оркестратор: python -m app {process,upload,status,retry}."""
import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
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
Style: Sub,Arial,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,2,80,80,400,1
Style: Title,Arial,90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,7,0,8,80,80,220,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
# Около 21 символа на строку при шрифте 80 и ширине 1080: так кусок не вылезает за две строки.
SUB_MAX_CHARS = 40
TITLE_SECONDS = 3.0


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


def write_ass(path: Path, cues: list[Cue], title: str) -> None:
    lines = [f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Sub,,0,0,0,,{ass_text(text)}" for a, b, text in cues]
    if title:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(TITLE_SECONDS)},Title,,0,0,0,,{ass_text(title)}")
    path.write_text(ASS_HEADER + "\n".join(lines) + "\n", encoding="utf-8")


def reencode(src: Path, dst: Path, ass: "Path | None" = None) -> None:
    cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-i", str(src)]
    if ass:
        # Имя без пути и cwd в папке клипа: в фильтрах ffmpeg двоеточие из C:\ надо экранировать.
        cmd += ["-vf", f"ass={ass.name}"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    subprocess.run(cmd, check=True, cwd=dst.parent)


def make_clips(source: str, out_dir: Path, num_clips: int) -> "list[tuple[Path, str]] | None":
    """Генератор и перекодирование в H.264. Список (клип, заголовок) или None, если генератор упал."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Генератору отдаём путь относительно его cwd: так он остаётся ASCII,
    # даже если в пути к проекту есть кириллица.
    out_rel = os.path.relpath(out_dir, config.GENERATOR_DIR)
    result_json = os.path.join(out_rel, "result.json")

    cmd = [str(config.GENERATOR_PYTHON), "main.py", source, "--mode", "local",
           "--num-clips", str(num_clips), "--output-json", result_json]
    if config.LANGUAGE:
        cmd += ["--language", config.LANGUAGE]
    log.info("Генератор: %s", out_dir)
    rc = subprocess.run(cmd, cwd=config.GENERATOR_DIR,
                        env={**os.environ, "LOCAL_OUTPUT_DIR": out_rel}).returncode
    if rc != 0:
        log.error("Генератор завершился с кодом %d", rc)
        return None

    result = json.loads((config.GENERATOR_DIR / result_json).read_text(encoding="utf-8"))
    shorts = result["shorts"]
    cues = []
    if config.SUBTITLES:
        # Генератор кэширует транскрипт исходника как <имя исходника>.srt в папке вывода.
        srt = out_dir / (Path(result["source_video_url"]).stem + ".srt")
        if srt.is_file():
            cues = read_srt(srt)
        else:
            log.warning("Нет %s, клипы будут без субтитров", srt.name)

    clips = []
    for short in shorts:
        if not short.get("clip_url"):
            log.warning("Пропуск, генератор не собрал клип: %s (%s)", short.get("title"), short.get("error"))
            continue
        raw = (config.GENERATOR_DIR / short["clip_url"]).resolve()
        clip = raw.with_name(raw.stem + "_h264.mp4")
        ass = None
        if config.SUBTITLES or config.HOOK_TITLE:
            ass = clip.with_suffix(".ass")
            write_ass(ass, clip_cues(cues, float(short["start_time"]), float(short["end_time"])),
                      short["title"] if config.HOOK_TITLE else "")
        try:
            reencode(raw, clip, ass)
        except subprocess.CalledProcessError as e:
            log.error("Пропуск, ffmpeg завершился с кодом %d: %s", e.returncode, raw.name)
            continue
        raw.unlink()
        if ass:
            ass.unlink()
        clips.append((clip, short["title"]))
    log.info("Готово клипов: %d из %d", len(clips), len(shorts))
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

    clips = make_clips(source, config.CLIPS_DIR / source_id(source, is_file), config.NUM_CLIPS)
    if clips is None:
        return 1
    db.executemany("INSERT INTO clips (path, caption, source) VALUES (?, ?, ?)",
                   [(str(path), title, source) for path, title in clips])
    db.commit()
    log.info("В очередь добавлено клипов: %d", len(clips))
    return 0


def cmd_cut(args: argparse.Namespace) -> int:
    raw = args.source or input("Ссылка или путь к видео: ").strip().strip('"')
    resolved = resolve_source(raw)
    if resolved is None:
        return 1
    source, is_file = resolved

    out_dir = config.PREVIEW_DIR / source_id(source, is_file)
    if make_clips(source, out_dir, config.CUT_NUM_CLIPS) is None:
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
    p = sub.add_parser("process", help="нарезать, перекодировать, добавить клипы в очередь")
    p.add_argument("source", help="путь к видео или ссылка")
    p.set_defaults(func=cmd_process)
    c = sub.add_parser("cut", help="только нарезать в data/preview, без очереди")
    c.add_argument("source", nargs="?", help="путь к видео или ссылка; без него спросит")
    c.set_defaults(func=cmd_cut)
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
