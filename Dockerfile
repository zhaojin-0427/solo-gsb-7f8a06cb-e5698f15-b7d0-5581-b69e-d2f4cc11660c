FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV SEARCH_DB=/data/search.db
VOLUME /data
EXPOSE 8000

# Single process by design: reader leases and the rebuild worker are
# in-process, and SQLite allows exactly one writer anyway.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
