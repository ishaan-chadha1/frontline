FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY taxonomy ./taxonomy

RUN pip install --no-cache-dir -e ".[api,gcp]" "psycopg[binary]"

ENV PORT=8080
CMD exec uvicorn frontline.app:app --host 0.0.0.0 --port ${PORT}
