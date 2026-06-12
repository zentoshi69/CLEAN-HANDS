FROM python:3.11-slim

# OpenCV / MediaPipe runtime libraries
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

# Temp storage for user images (purged by the cleanup job)
RUN mkdir -p /tmp/clean-hands-bot

CMD ["python", "-m", "src.bot.main"]
