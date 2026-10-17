"""Один запрос к Gemini: промпт из stdin, JSON-ответ в stdout.

Запускается python из venv генератора с cwd генератора: там уже есть google-genai, а в его .env
лежат ключ и модель. В оркестратор не импортируется.
"""
import os
import sys

from dotenv import load_dotenv
from google import genai

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(".env")  # без пути ищет от папки этого скрипта, а не от cwd генератора
# Как в патче генератора: без http_options google-genai ждёт вечно и не повторяет 503.
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                      http_options={"timeout": 120_000, "retry_options": {"attempts": 5}})
response = client.models.generate_content(model=os.environ["GEMINI_MODEL"], contents=sys.stdin.read(),
                                          config={"response_mime_type": "application/json"})
if response.prompt_feedback and response.prompt_feedback.block_reason:
    print(f"Промпт заблокирован фильтром Gemini: {response.prompt_feedback.block_reason}", file=sys.stderr)
    sys.exit(3)  # оркестратор отличает блок от прочих ошибок по этому коду
if not response.text:
    sys.exit(f"Пустой ответ Gemini: {[c.finish_reason for c in response.candidates or []]}")
print(response.text)
