"""Оркестратор: python -m app {process,upload,status,retry}."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from . import config

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

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


def source_id(source: str, is_file: bool) -> str:
    # Только ASCII: путь к папке уходит в OpenCV, а тот на Windows не всегда
    # открывает файлы с кириллицей.
    stem = Path(source).stem if is_file else "url"
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:40] or "video"
    return f"{stem}_{hashlib.sha1(source.encode()).hexdigest()[:8]}"


def reencode(src: Path, dst: Path) -> None:
    subprocess.run(
        [config.FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)],
        check=True,
    )


def cmd_process(args) -> int:
    is_file = Path(args.source).is_file()
    if is_file:
        source = str(Path(args.source).resolve())
    elif args.source.startswith(("http://", "https://")):
        source = args.source
    else:
        print(f"Файл не найден: {args.source}", file=sys.stderr)
        return 1

    db = connect()
    if db.execute("SELECT 1 FROM clips WHERE source = ?", (source,)).fetchone():
        print(f"Уже обработан, клипы в очереди: {source}")
        return 0

    out_dir = config.CLIPS_DIR / source_id(source, is_file)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Генератору отдаём путь относительно его cwd: так он остаётся ASCII,
    # даже если в пути к проекту есть кириллица.
    out_rel = os.path.relpath(out_dir, config.GENERATOR_DIR)
    result_json = os.path.join(out_rel, "result.json")

    cmd = [str(config.GENERATOR_PYTHON), "main.py", source, "--mode", "local",
           "--num-clips", str(config.NUM_CLIPS), "--output-json", result_json]
    if config.LANGUAGE:
        cmd += ["--language", config.LANGUAGE]
    print(f"Генератор → {out_dir}", flush=True)
    rc = subprocess.run(cmd, cwd=config.GENERATOR_DIR,
                        env={**os.environ, "LOCAL_OUTPUT_DIR": out_rel}).returncode
    if rc != 0:
        print(f"Генератор завершился с кодом {rc}", file=sys.stderr)
        return 1

    shorts = json.loads((config.GENERATOR_DIR / result_json).read_text(encoding="utf-8"))["shorts"]
    added = 0
    for short in shorts:
        if not short.get("clip_url"):
            print(f"Пропуск (генератор не собрал клип): {short.get('title')} — {short.get('error')}")
            continue
        raw = (config.GENERATOR_DIR / short["clip_url"]).resolve()
        clip = raw.with_name(raw.stem + "_h264.mp4")
        try:
            reencode(raw, clip)
        except subprocess.CalledProcessError as e:
            print(f"Пропуск (ffmpeg, код {e.returncode}): {raw.name}", file=sys.stderr)
            continue
        raw.unlink()
        db.execute("INSERT INTO clips (path, caption, source) VALUES (?, ?, ?)",
                   (str(clip), short["title"], source))
        added += 1
    db.commit()
    print(f"В очередь добавлено клипов: {added} из {len(shorts)}")
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
    sub.add_parser("upload", help="залить pending-клипы в свободные слоты").set_defaults(func=cmd_upload)
    sub.add_parser("status", help="показать очередь").set_defaults(func=cmd_status)
    r = sub.add_parser("retry", help="вернуть failed/unknown в pending")
    r.add_argument("ids", nargs="+", type=int)
    r.set_defaults(func=cmd_retry)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
