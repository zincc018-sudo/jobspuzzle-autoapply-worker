# JobsPuzzle auto-apply worker — runs the vision agent on a persistent datacenter box.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# fonts + CA certs so headless Chromium renders real pages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-liberation libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt
# fetch the Chromium browser-use drives (with its OS deps)
RUN playwright install --with-deps chromium

COPY agent.py worker.py ./

# background worker: polls Supabase, fills forms, never submits
CMD ["python", "worker.py"]
