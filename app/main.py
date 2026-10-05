from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import limits
from .config import get_settings
from .middleware import UploadSizeLimitMiddleware
from .routers import chat, chat_multimodal, document_extract, files, history

settings = get_settings()

app = FastAPI(title="Kerneu AI Platform", version="0.1.0")

# Ранний отказ 413 для слишком больших загрузок. Добавляется ДО CORS, чтобы CORS
# оставался внешним слоем и 413 дошёл до браузера с CORS-заголовками.
app.add_middleware(
    UploadSizeLimitMiddleware,
    limits_by_path={
        "/v1/documents/extract": limits.MAX_DOCUMENT_BYTES + limits.UPLOAD_ENVELOPE_OVERHEAD,
        "/v1/chat/multimodal": limits.MAX_TOTAL_UPLOAD_BYTES + limits.UPLOAD_ENVELOPE_OVERHEAD,
    },
)

# Fail-closed по умолчанию: если CORS_ALLOWED_ORIGINS не задан в .env,
# cors_origins_list будет пустым списком — allow_origins=[] значит браузер
# заблокирует все cross-origin запросы к API, пока явно не перечислите
# домен ERP-фронтенда.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router, tags=["chat"])
app.include_router(chat_multimodal.router, tags=["chat"])
app.include_router(history.router, tags=["history"])
app.include_router(document_extract.router, tags=["documents"])
app.include_router(files.router, tags=["files"])


@app.get("/health")
async def health():
    return {"status": "ok"}
