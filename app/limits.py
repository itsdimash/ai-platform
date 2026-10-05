"""Лимиты сервиса в одном месте.

Провайдерские лимиты сверены с официальной документацией (октябрь 2026):
- Anthropic: изображение до 10 МБ в base64 (≈7,5 МБ сырых), сторона до 8000 px;
  запрос до 32 МБ; PDF до 32 МБ, до 600 страниц (100 — на моделях с контекстом 200K).
- OpenAI: до 512 МБ на запрос; нативного PDF в Chat Completions нет.
- Gemini: запрос с inline-данными до 100 МБ (больше — Files API); PDF до 50 МБ / 1000 страниц.
Base64 раздувает данные в 4/3 раза, поэтому «сырые» бюджеты ниже документных лимитов.
"""

MB = 1024 * 1024

# --- Загрузки пользователя ----------------------------------------------------
CHUNK_SIZE = MB  # файлы читаются кусками по 1 МБ с остановкой при превышении
MAX_DOCUMENT_BYTES = 50 * MB  # pdf / docx / xlsx
MAX_IMAGE_BYTES = 20 * MB
MAX_FILES_PER_REQUEST = 10
MAX_TOTAL_UPLOAD_BYTES = 100 * MB
MAX_ATTACHMENT_KEYS = 10
# multipart-обвязка + поля формы поверх самих файлов (для раннего отказа в middleware)
UPLOAD_ENVELOPE_OVERHEAD = 2 * MB

# Текст, извлекаемый из документов и вклеиваемый в промпт.
MAX_EXTRACTED_CHARS_PER_FILE = 60_000
MAX_EXTRACTED_CHARS_TOTAL = 200_000

# --- Генерация файлов ---------------------------------------------------------
MAX_SLIDES = 100  # потолок слайдов в колоде (после разбиений)
MAX_XLSX_ROWS = 50_000  # потолок строк на лист xlsx
DECK_MAX_IMAGES_HARD = (
    8  # жёсткий потолок картинок в колоде (config.yaml -> deck.max_images не выше)
)
DECK_TABLE_ROWS_PER_SLIDE = 8  # строк данных в таблице слайда (остальное — на следующий слайд)
DECK_TABLE_MAX_COLS = 6
DECK_CHART_MAX_CATEGORIES = 12
DECK_CHART_MAX_SERIES = 4
DECK_MAX_BULLETS = 5  # рекомендуемый максимум пунктов на слайде (больше — делим слайд)

# --- Anthropic ----------------------------------------------------------------
ANTHROPIC_IMAGE_MAX_RAW_BYTES = 7_500_000  # 10 МБ base64
ANTHROPIC_IMAGE_MAX_EDGE = 8000
ANTHROPIC_REQUEST_RAW_BUDGET = 23 * MB  # 32 МБ запрос / (4/3) минус промпт и история
ANTHROPIC_PDF_MAX_PAGES = 600
ANTHROPIC_PDF_MAX_PAGES_200K_CONTEXT = 100  # Haiku 4.5

# --- Gemini -------------------------------------------------------------------
GEMINI_REQUEST_RAW_BUDGET = 65 * MB  # 100 МБ запрос / (4/3) минус промпт и история
GEMINI_PDF_MAX_BYTES = 50 * MB
GEMINI_PDF_MAX_PAGES = 1000

# --- OpenAI -------------------------------------------------------------------
OPENAI_REQUEST_RAW_BUDGET = 350 * MB  # 512 МБ запрос / (4/3)

# Ступени уменьшения изображений (длинная сторона, px), когда суммарный
# payload не помещается в бюджет запроса.
IMAGE_SHRINK_LADDER = (4096, 3072, 2048, 1568, 1024)
IMAGE_SHRINK_MIN_BYTES = 256 * 1024  # мельче не трогаем
