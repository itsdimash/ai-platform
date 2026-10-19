FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
# uv.lock копируется и применяется строго (--frozen): сборка воспроизводима.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# ВАЖНО: --workers НЕ добавлять. Запуски шаблонов (app/templates/runner.py) исполняются
# asyncio-задачами внутри процесса: ссылки на задачи (для отмены), лимит активных run и
# семафоры живут в памяти ОДНОГО процесса. Несколько воркеров сломают отмену и лимиты.
# --frozen --no-dev: при старте контейнера uv не пересобирает окружение и не
# тянет dev-зависимости.
CMD ["uv", "run", "--frozen", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
