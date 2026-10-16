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
# Клипы: все кандидаты генератора со score от CLIP_MIN_SCORE, но не меньше CLIPS_MIN лучших и не
# больше CLIPS_MAX. Модель ставит оценки в узком диапазоне (по 52 кандидатам 78-96, половина в
# 85-89), 90 и выше это верхние ~40%. CLIPS_MAX = 2 слота в день на 10 дней вперёд (MAX_LEAD).
CLIP_MIN_SCORE = 90
CLIPS_MIN = 2
CLIPS_MAX = 20
LANGUAGE = "ru"  # None = автоопределение Whisper
# Критерии выбора моментов и правила заголовков: патч генератора подставляет этот файл в промпт.
HIGHLIGHTS_PROMPT = ROOT / "prompts" / "highlights.txt"
# Страховка от зависшего Gemini: выбор моментов дольше HIGHLIGHTS_TIMEOUT плюс
# HIGHLIGHTS_TIMEOUT_PER_HOUR на каждый час видео убивает генератор. С патчем один запрос живёт
# до ~10 минут (5 попыток по 2 минуты), без патча ждёт вечно. Обычно 3 часа разбираются за ~2 минуты.
HIGHLIGHTS_TIMEOUT = timedelta(minutes=10)
HIGHLIGHTS_TIMEOUT_PER_HOUR = timedelta(minutes=5)

UPLOADER_DIR = ROOT / "tools" / "tiktok-uploader"
UPLOADER_PYTHON = UPLOADER_DIR / _VENV_PYTHON
TIKTOK_ACCOUNT = "main"  # имя из `cli.py login -n`

FFMPEG = "ffmpeg"
OUTPUT_FPS = 30    # потолок частоты кадров клипа: на 60 fps исходника монтаж идёт примерно на треть дольше
SUBTITLES = False  # вжигать субтитры из транскрипта генератора
HOOK_TITLE = True  # вжигать заголовок клипа из result.json
LOUDNESS = -14     # LUFS, громкость клипа после loudnorm
SNAP_MAX = 2.0     # секунд: граница реплики дальше этого не притягивает начало или конец клипа
SNAP_PAD = 0.3     # секунд запаса после подгонки границ клипа к репликам

# Заголовок. Шрифт ищется в FONTS_DIR по имени семейства, цвет в формате ASS &H00BBGGRR.
FONTS_DIR = ROOT / "fonts"
TITLE_SECONDS = None        # None: весь клип, число: только первые N секунд
TITLE_FONT = "Russo One"
TITLE_SIZE = 96             # при длинном заголовке шрифт уменьшается, чтобы влезть в две строки
TITLE_COLOR = "&H0000FFFF"  # ярко-жёлтый
TITLE_OUTLINE = 10

# Безопасная зона TikTok в кадре 1080x1920, px: снаружи интерфейс (вкладки, кнопки, ник,
# описание). Официальной цифры для обычных постов нет, советы расходятся: сверху 120-240,
# снизу 250-660, справа 150-180. Субтитры и заголовок ставятся внутрь зоны.
SAFE_TOP = 160
SAFE_BOTTOM = 480
SAFE_LEFT = 60
SAFE_RIGHT = 150

LAYOUT = "auto"  # режим по умолчанию: auto (стримы), fit (фильм: весь кадр), crop (фильм: обрезка)

# auto: лица ищет YuNet раз в AUTO_SAMPLE_SECONDS. Размеры и координаты в долях кадра исходника.
FACE_MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
AUTO_SAMPLE_SECONDS = 1.0
AUTO_FACE_SCORE = 0.7      # уверенность детектора
AUTO_MIN_SHARE = 0.6       # лицо должно найтись хотя бы в такой доле замеров клипа; в нарезках вебка
                           # часто есть только в части клипа, и тогда безопаснее fit
AUTO_STABLE = 0.04         # вебка: лицо сдвигается между замерами не дальше этого
AUTO_EDGE = 0.2            # вебка: центр лица дальше этого от центра кадра по x
AUTO_STILL = 2.0           # движение в рамке лица меньше этого (0..255): неподвижная картинка, не вебка
AUTO_BIG_FACE = 0.15       # высота лица от высоты кадра: от этого и больше считаем IRL/подкаст
AUTO_CENTER = 0.25         # IRL: центр лица не дальше этого от центра кадра по x
AUTO_SMALL_FACE = 0.1      # лицо ниже этого считаем лицом в маленькой вебке
AUTO_IRL_SHARE = 0.5       # IRL в движении: заметное лицо не у края хотя бы в такой доле замеров
AUTO_CAM_SCALE = 2.6       # высота рамки вебки в высотах лица
AUTO_CAM_FACE_Y = 0.55     # центр лица на такой доле высоты рамки вебки: в вебке ниже лица плечи
AUTO_SMOOTH = 5            # кадр по лицу: окно сглаживания в замерах

# fit и crop: чёрные полосы фильма внутри 16:9 срезаются до монтажа. cropdetect смотрит столько
# точек клипа, рамка берётся по всем сразу, чтобы тёмная сцена не отрезала часть картинки.
BARS_POINTS = 5

# crop: окно 9:16 держит в кадре главное лицо. Время в секундах.
CROP_SAMPLE_FPS = 3        # замеров лиц в секунду
CROP_SCENE = 0.3           # порог склейки для scene detection ffmpeg (0..1), выше значит меньше склеек
CROP_SCENE_MIN = 1.0       # сцена короче этого присоединяется к соседней
CROP_FACE_SHARE = 0.2      # лицо нашлось меньше чем в такой доле замеров сцены: сцена по центру
CROP_DEADZONE = 0.15       # кадр стоит, пока цель не дальше этого от центра окна, в ширинах окна
CROP_SMOOTH = 0.4          # плавность: постоянная времени каждого из двух сглаживаний движения
CROP_SWITCH = 1.3          # другое лицо становится главным, если оно во столько раз крупнее
CROP_SWITCH_SECONDS = 1.0  # и остаётся крупнее хотя бы столько
CROP_LOST_HOLD = 1.5       # лицо пропало: столько держим прошлое место, потом центр
CROP_PAIR = 0.6            # второе лицо держим в окне вместе с главным, если оно не меньше такой доли
                           # главного по высоте и оба влезают в окно

# Слоты публикации, каждый день, по местному времени.
SLOTS = ["12:00", "19:00"]
# Владивосток, UTC+10 без перехода на летнее время. Фиксированный сдвиг вместо
# ZoneInfo: на Windows для ZoneInfo нужен пакет tzdata.
TZ = timezone(timedelta(hours=10), "Asia/Vladivostok")

# TikTok принимает расписание от 15 минут; +5 минут на саму заливку, иначе autotok
# сдвинет публикацию с границы слота.
MIN_LEAD = timedelta(minutes=20)
MAX_LEAD = timedelta(days=10)
