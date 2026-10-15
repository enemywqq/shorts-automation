import os
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db.sqlite"
CLIPS_DIR = ROOT / "data" / "clips"
PREVIEW_DIR = ROOT / "data" / "preview"

_VENV_PYTHON = Path(".venv", "Scripts", "python.exe") if os.name == "nt" else Path(".venv", "bin", "python")

GENERATOR_DIR = ROOT / "tools" / "shorts-generator"
GENERATOR_PYTHON = GENERATOR_DIR / _VENV_PYTHON
NUM_CLIPS = 3
CUT_NUM_CLIPS = 5  # для cut
LANGUAGE = "ru"  # None = автоопределение Whisper

UPLOADER_DIR = ROOT / "tools" / "tiktok-uploader"
UPLOADER_PYTHON = UPLOADER_DIR / _VENV_PYTHON
TIKTOK_ACCOUNT = "main"  # имя из `cli.py login -n`

FFMPEG = "ffmpeg"

# Слоты публикации, каждый день, по местному времени.
SLOTS = ["12:00", "19:00"]
# Владивосток, UTC+10 без перехода на летнее время. Фиксированный сдвиг вместо
# ZoneInfo: на Windows для ZoneInfo нужен пакет tzdata.
TZ = timezone(timedelta(hours=10), "Asia/Vladivostok")

# TikTok принимает расписание от 15 минут; +5 минут на саму заливку, иначе autotok
# сдвинет публикацию с границы слота.
MIN_LEAD = timedelta(minutes=20)
MAX_LEAD = timedelta(days=10)
