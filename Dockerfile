FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for lxml, matplotlib, and curl_cffi
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV DATABASE_PATH=/app/data/lego_tracker.db

CMD ["python", "-m", "bot.main"]
