FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FTS_DB_PATH=/data/fts.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY docker/healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod +x /usr/local/bin/healthcheck.sh && mkdir -p /data

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD /usr/local/bin/healthcheck.sh

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
