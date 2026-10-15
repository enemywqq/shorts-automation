# shorts-automation

Оркестратор: исходное видео → вертикальные клипы → очередь → публикация в TikTok по расписанию.

Сам ничего не режет и не заливает: вызывает два внешних инструмента через `subprocess`, у каждого свой venv.

| Инструмент | Папка | Что делает |
|---|---|---|
| [AI-Youtube-Shorts-Generator](https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator) | `tools/shorts-generator` | видео → `short_NN.mp4` + JSON с заголовками/таймингами/скором |
| [TiktokAutoUploader](https://github.com/makiisthenes/TiktokAutoUploader) (autotok 2.x) | `tools/tiktok-uploader` | заливка клипа на аккаунт, сейчас или с отложенной публикацией |

## Структура

```
tools/               внешние репо (git submodules), у каждого свой .venv
data/input/          исходные видео
data/clips/<id>/     клипы одного исходника (H.264) + result.json генератора
data/preview/<id>/   то же для cut, без очереди
cut.bat              cut перетаскиванием файла или по ссылке
app/config.py        все настройки: слоты, аккаунт, число клипов, язык
app/__main__.py      CLI оркестратора
db.sqlite            очередь: таблица clips
```

## Оркестратор

Нужен только Python 3.11+ (одна стандартная библиотека, свой venv не нужен) и `ffmpeg` в PATH. Оба инструмента должны быть установлены по разделам ниже. Запуск из корня проекта:

```powershell
py -3.13 -m app process data\input\video.mp4      # нарезать → перекодировать в H.264/AAC → в очередь
py -3.13 -m app process "https://youtu.be/..."    # то же для ссылки
py -3.13 -m app upload                            # залить все pending в свободные слоты
py -3.13 -m app status                            # очередь
py -3.13 -m app retry 3 4                         # вернуть failed/unknown в pending
```

Расписание держит TikTok: `upload` сразу заливает клип с `-sc`, компьютер после этого может быть выключен.

**Просто нарезать.** `cut` режет и перекодирует так же, как `process`, но в очередь ничего не пишет. Клипы складываются в `data\preview\<имя исходника>\`, по окончании папка открывается в проводнике. Число клипов задаёт `CUT_NUM_CLIPS` (по умолчанию 5).

```powershell
py -3.13 -m app cut data\input\video.mp4
py -3.13 -m app cut                               # спросит ссылку или путь
```

То же без консоли: перетащи видеофайл на `cut.bat` в корне проекта или запусти его двойным кликом и вставь ссылку. Окно остаётся открытым, пока не нажмёшь клавишу.

`process` и `cut` принимают и ссылку на YouTube, генератор скачает ролик через yt-dlp. Для ссылки имя папки берётся из id ролика.

**Субтитры и заголовок.** При перекодировании в клип вжигаются субтитры из транскрипта генератора (белый жирный шрифт с чёрной обводкой, нижняя треть кадра, не больше двух строк) и заголовок из `result.json` вверху кадра на первые 3 секунды. Выключаются флагами `SUBTITLES` и `HOOK_TITLE` в `app/config.py`. Размер шрифта и отступы лежат в `ASS_HEADER` в `app/__main__.py`.

**Слоты.** `SLOTS` в [app/config.py](app/config.py) — время на каждый день по Владивостоку. Клипу достаётся ближайший слот, который не занят клипом в статусе `uploaded`/`unknown`, начинается не раньше чем через 20 минут (минимум TikTok — 15, плюс 5 минут на саму заливку) и не дальше 10 дней. Нет такого слота — клип ждёт следующего `upload`.

**Статусы.**
- `pending` — ждёт заливки.
- `uploaded` — TikTok принял, публикация в указанный слот.
- `failed` — не ушло (TikTok отклонил, сессия истекла, файл пропал). Слот освобождается. Текст ошибки виден в `status`.
- `unknown` — autotok сообщил, что пост мог уйти, или заливка была прервана (Ctrl+C, падение). Автоматически не повторяется никогда: проверь аккаунт в TikTok Studio и, если видео там нет, сделай `retry <id>`.

Повторный `process` того же исходника ничего не делает, если его клипы уже в очереди.

## Клонирование

```powershell
git clone --recurse-submodules https://github.com/enemywqq/shorts-automation.git
# или, если уже склонировано без сабмодулей:
git submodule update --init
```

Все команды ниже — PowerShell на Windows, из корня проекта. Python 3.11+ (3.13 подходит обоим).
venv не активируем, а вызываем `.venv\Scripts\python` напрямую — так не мешает ExecutionPolicy.

---

## 1. Генератор (tools/shorts-generator)

### Установка

```powershell
winget install Gyan.FFmpeg          # ffmpeg нужен в PATH; после установки открой новый терминал
ffmpeg -version                     # проверка

cd tools\shorts-generator
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-local.txt "av==18.1.0" "opencv-python<5"
copy .env.example .env
```

Две версии закреплены вручную, потому что требования генератора не ограничены сверху:
- PyAV 18.1.0. faster-whisper 1.2.1 (последний на октябрь 2026) открывает видео через `av.open(..., metadata_errors="ignore")`, а в av 19 такого параметра нет, и распознавание падает с `open() got an unexpected keyword argument 'metadata_errors'`. Закрепление можно снять, когда выйдет faster-whisper новее 1.2.1 с исправлением.
- opencv-python 4.x. В OpenCV 5 нет `cv2.CascadeClassifier`, на котором генератор ищет лица при кадрировании. Клип падает, файл `short_NN.mp4.cut.mp4` остаётся открытым, и в `result.json` вместо настоящей ошибки попадает `WinError 32`.

В `.env` для local-режима достаточно (MUAPI-строки можно удалить):

```ini
LLM_PROVIDER=gemini                 # или openai
GEMINI_API_KEY=...                  # или OPENAI_API_KEY=...
LOCAL_WHISPER_MODEL=small           # base по умолчанию; для русской речи small заметно лучше
LOCAL_WHISPER_DEVICE=cpu
LOCAL_OUTPUT_DIR=output
LOCAL_WHISPER_VAD_FILTER=true       # без него Whisper выдумывает титры на паузах и музыке
```

### Запуск

Положи видео в `data\input\`, затем из `tools\shorts-generator`:

```powershell
.\.venv\Scripts\python main.py "..\..\data\input\video.mp4" --mode local --num-clips 2 --language ru --output-json output\result.json
```

Первый запуск скачает модель Whisper с HuggingFace. На CPU транскрипция идёт примерно в реальном времени — для проверки бери ролик на 5–10 минут.

Ожидаемый результат в `tools\shorts-generator\output\`:
- `short_01.mp4`, `short_02.mp4` — вертикальные клипы 9:16;
- `video.srt` — кэш транскрипта (повторный запуск по тому же файлу пропустит Whisper);
- `result.json` — `shorts[]` с полями `title`, `start_time`, `end_time`, `score`, `hook_sentence`, `virality_reason`, `clip_url`.

Флаги: `--mode` (**по умолчанию `api`**, всегда передавай `local`), `--num-clips` (3), `--aspect-ratio` (9:16), `--format` (720, только для YouTube), `--language` (авто), `--output-json`.

---

## 2. Аплоадер (tools/tiktok-uploader)

### Установка

```powershell
cd tools\tiktok-uploader
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python cli.py install-browser          # Chromium для Playwright, ~300 МБ
```

Node.js не нужен.

### Логин (один раз)

```powershell
.\.venv\Scripts\python cli.py login -n main
```

Откроется окно Chromium с логином TikTok — быстрее всего через QR из приложения. Окно закроется само, сессия сохранится в `%USERPROFILE%\.autotok\accounts\main`.

```powershell
.\.venv\Scripts\python cli.py accounts check main      # "session OK"
```

### Тестовая заливка

Сначала приватно, чтобы никто не увидел:

```powershell
.\.venv\Scripts\python cli.py upload -u main -v "..\shorts-generator\output\short_01.mp4" -t "test" -vi 1
```

Затем отложенная (приватные TikTok планировать не даёт, поэтому эта будет публичной — удали её в приложении до публикации, если не нужна):

```powershell
.\.venv\Scripts\python cli.py upload -u main -v "..\shorts-generator\output\short_01.mp4" -t "test #fyp" -sc 3600
```

Успех: код выхода 0 и строка `Published (scheduled for ...). Video id: ...`. Запланированное видео видно в TikTok Studio.

---

## Что в инструментах не совпало с исходным описанием

**Генератор** — в целом как описано, но:
- режим по умолчанию `api` (платный MuAPI), `--mode local` обязателен;
- имена `short_01.mp4…` фиксированные и **перезаписываются** при каждом запуске; папка вывода относительна cwd (`LOCAL_OUTPUT_DIR`), туда же кладутся скачанные исходники и `.srt`;
- JSON пишется только при успехе всего прогона; отдельный клип может упасть (`clip_url: null`, `error`), а код выхода всё равно 0;
- промпт LLM на английском — заголовки, скорее всего, будут английскими даже для русского видео;
- итоговое видео в кодеке **mp4v (MPEG-4 Part 2), не H.264**: OpenCV пишет mp4v, ffmpeg копирует поток без перекодирования. TikTok рекомендует H.264 — проверь на тестовой заливке. Если будут проблемы, в склейке перекодируем одним вызовом ffmpeg.

**Аплоадер** — репо переписан (сейчас это пакет `autotok` 2.0.1):
- Node.js больше не нужен — подписи считаются в headless Chromium;
- `VideosDirPath` не нужен: `-v` принимает любой путь; голое имя файла ищется в `~/.autotok/videos` и `./VideosDirPath`;
- сессии лежат в `~/.autotok/accounts` (переопределяется `AUTOTOK_HOME`), а не в `CookiesDir`;
- `cli.py login -n` и `cli.py upload --user -v -t` работают как раньше (оставлены для совместимости);
- `-sc` — секунды от текущего момента, 900…2592000 (30 дней), но части аккаунтов TikTok разрешает только 10 дней (864000); приватные видео планировать нельзя;
- подпись — до 2200 символов, хэштеги и @упоминания кликабельны;
- коды выхода: 0 — ок, 2 — ошибка валидации, 1 — прочие ошибки;
- если сеть упала на шаге публикации, autotok сообщает, что пост «may or may not have been posted». Такую задачу нельзя слепо ретраить — иначе будет дубль.
