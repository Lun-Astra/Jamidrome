FROM python:3.12-slim
RUN apt-get update && apt-get install -y \
    ffmpeg openssh-client rsync curl unzip \
    && rm -rf /var/lib/apt/lists/*
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN mkdir -p /data/downloads
EXPOSE 5000
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--timeout", "300", "--bind", "0.0.0.0:5000", "app:app"]
