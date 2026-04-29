FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp

COPY . .

RUN mkdir -p clips

ENV PORT=8080
CMD ["/bin/sh", "-c", "exec gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300"]
