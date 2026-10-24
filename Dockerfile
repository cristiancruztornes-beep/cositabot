FROM python:3.11-slim

# ffmpeg para unir audio y video, chromium + chromedriver para Selenium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg chromium chromium-driver ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER=/usr/bin/chromedriver \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot_picta.py .

CMD ["python", "bot_picta.py"]
