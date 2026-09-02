from fastapi import FastAPI

from .routers import chat, history

app = FastAPI(title="Kerneu AI Platform", version="0.1.0")

app.include_router(chat.router, tags=["chat"])
app.include_router(history.router, tags=["history"])


@app.get("/health")
async def health():
    return {"status": "ok"}
