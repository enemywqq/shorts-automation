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
fonts/               шрифт заголовка (Russo One, OFL)
patches/             патч генератора
app/config.py        все настройки: слоты, аккаунт, число клипов, язык
app/__main__.py      CLI оркестратора
db.sqlite            очередь: таблица clips
```

## Оркестратор

Нужен только Python 3.11+ (одна стандартная библиотека, свой venv не нужен) и `ffmpeg` в PATH. Оба инструмента должны быть установлены по разделам ниже. Запуск из корня проекта:

```powershell
py -3.13 -m app process data\input\video.mp4      # выбрать моменты → смонтировать 1080x1920 → в очередь
py -3.13 -m app process "https://youtu.be/..."    # то же для ссылки
py -3.13 -m app upload                            # залить все pending в свободные слоты
py -3.13 -m app status                            # очередь
py -3.13 -m app retry 3 4                         # вернуть failed/unknown в pending
```

Расписание держит TikTok: `upload` сразу заливает клип с `-sc`, компьютер после этого может быть выключен.

**Просто нарезать.** `cut` делает то же, что `process`, но в очередь ничего не пишет. Клипы складываются в `data\preview\<имя исходника>\`, по окончании папка открывается в проводнике. Число клипов задаёт `CUT_NUM_CLIPS` (по умолчанию 5).

```powershell
py -3.13 -m app cut data\input\video.mp4
py -3.13 -m app cut                               # спросит ссылку или путь
```

То же без консоли: перетащи видеофайл на `cut.bat` в корне проекта или запусти его двойным кликом и вставь ссылку. Потом bat спросит макет, а для split ещё пресет вебки. Окно остаётся открытым, пока не нажмёшь клавишу.

`process` и `cut` принимают и ссылку на YouTube, генератор скачает ролик через yt-dlp. Для ссылки имя папки берётся из id ролика.

Пока генератор молчит, раз в минуту в консоль пишется текущий этап и сколько он уже идёт. В конце печатается время по этапам: транскрипция, выбор моментов, монтаж. Если выбор моментов длится дольше `HIGHLIGHTS_TIMEOUT` (10 минут), генератор останавливается с ошибкой.

**Макеты.** Генератор только расшифровывает речь и выбирает моменты, клипы 1080x1920 монтирует оркестратор, по одному вызову ffmpeg на клип. Макет задаёт `--layout` у `cut` и `process`:
- `fit` (по умолчанию, `LAYOUT` в конфиге): весь кадр по центру, сверху и снизу тот же кадр, растянутый и размытый;
- `split`: вверху вебка, внизу центр экрана (по высоте 40 на 60). Прямоугольник вебки берётся из пресета: `--cam evelone`;
- `crop`: обрезка по центру кадра.

```powershell
py -3.13 -m app cut video.mp4 --layout split --cam evelone
```

Пресеты лежат в `CAM_PRESETS` в `app/config.py`: имя и `x, y, w, h` в долях кадра, поэтому один пресет подходит к любому разрешению стрима.

**Субтитры, заголовок, звук.** В клип вжигаются субтитры из транскрипта генератора (белый жирный шрифт с чёрной обводкой, не больше двух строк) и заголовок из `result.json`. Заголовок висит весь клип: ярко-жёлтый Russo One с толстой чёрной обводкой, не больше двух строк, длинный заголовок набирается мельче. Шрифт лежит в `fonts/` вместе с лицензией OFL, ffmpeg получает его через `fontsdir`. Время показа, шрифт, размер, цвет и обводка заголовка задаются в `app/config.py` (`TITLE_*`).

Где что стоит:
- fit: заголовок на верхней размытой полосе, субтитры на нижней;
- split: заголовок в верхней части экрана сразу под вебкой, субтитры у низа экрана, лицо и прицел в центре остаются открытыми;
- crop: заголовок вверху, субтитры в нижней трети.

Выключаются флагами `SUBTITLES` и `HOOK_TITLE`. Позиции лежат в `SUB_POS` и `TITLE_POS` в `app/__main__.py`.

Начало клипа подтягивается к ближайшему началу реплики, конец к ближайшему концу, если граница не дальше 2 с (`SNAP_MAX`). Иначе остаётся время генератора. Затем добавляется 0.3 с запаса (`SNAP_PAD`). Громкость выравнивается loudnorm до -14 LUFS (`LOUDNESS`).

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

Затем накати патч генератора (следующий раздел).

## Патч генератора

В `patches/shorts-generator.patch` две правки генератора:
- у запросов к Gemini таймаут 2 минуты и до 5 попыток с паузами 1, 2, 4, 8 с. Без этого google-genai ждёт ответа вечно, а один ответ 503 «high demand» роняет весь прогон;
- env `LOCAL_SKIP_CROP=true` отключает его нарезку и кадрирование: Haar на стримах прыгал за ложными лицами, а клипы теперь монтирует оркестратор. Оркестратор всегда передаёт этот env, без патча генератор будет кадрировать впустую.

Патч накатывается после клонирования и после каждого обновления сабмодуля:

```powershell
git -C tools\shorts-generator checkout .                                  # снять старый патч
git submodule update --init
git -C tools\shorts-generator apply ..\..\patches\shorts-generator.patch
```

Если патч на месте, `git -C tools\shorts-generator apply --reverse --check ..\..\patches\shorts-generator.patch` ничего не выводит. Если после обновления `apply` падает, значит, генератор поменял эти файлы. Тогда правки переносятся вручную, а патч пересобирается:

```powershell
git -C tools\shorts-generator diff --output=..\..\patches\shorts-generator.patch
```

`--output` вместо `>`: Windows PowerShell 5.1 пишет перенаправление в UTF-16, и такой патч git не примет.

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
- opencv-python 4.x. В OpenCV 5 нет `cv2.CascadeClassifier`, на котором генератор ищет лица при кадрировании. Клип падает, файл `short_NN.mp4.cut.mp4` остаётся открытым, и в `result.json` вместо настоящей ошибки попадает `WinError 32`. С патчем кадрирование не запускается, закрепление нужно для запуска генератора без патча.

В `.env` для local-режима достаточно (MUAPI-строки можно удалить):

```ini
LLM_PROVIDER=gemini                 # или openai
GEMINI_API_KEY=...                  # или OPENAI_API_KEY=...
GEMINI_MODEL=gemini-3.5-flash-lite  # полные flash-модели часто отвечают 503 high demand
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
- итоговое видео в кодеке **mp4v (MPEG-4 Part 2), не H.264**: OpenCV пишет mp4v, ffmpeg копирует поток без перекодирования. Оркестратор его видео не использует: клипы в H.264 он монтирует из исходника сам.

**Аплоадер** — репо переписан (сейчас это пакет `autotok` 2.0.1):
- Node.js больше не нужен — подписи считаются в headless Chromium;
- `VideosDirPath` не нужен: `-v` принимает любой путь; голое имя файла ищется в `~/.autotok/videos` и `./VideosDirPath`;
- сессии лежат в `~/.autotok/accounts` (переопределяется `AUTOTOK_HOME`), а не в `CookiesDir`;
- `cli.py login -n` и `cli.py upload --user -v -t` работают как раньше (оставлены для совместимости);
- `-sc` — секунды от текущего момента, 900…2592000 (30 дней), но части аккаунтов TikTok разрешает только 10 дней (864000); приватные видео планировать нельзя;
- подпись — до 2200 символов, хэштеги и @упоминания кликабельны;
- коды выхода: 0 — ок, 2 — ошибка валидации, 1 — прочие ошибки;
- если сеть упала на шаге публикации, autotok сообщает, что пост «may or may not have been posted». Такую задачу нельзя слепо ретраить — иначе будет дубль.
