FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libpango-1.0-0 \
    libharfbuzz0b \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libffi-dev \
    libssl-dev \
    libcairo2 \
    libgdk-pixbuf-xlib-2.0-0 \
    shared-mime-info \
    poppler-utils \
    curl \
    fonts-liberation \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium
# Use --with-deps but pre-install fonts-unifont to avoid the ttf-unifont rename issue
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-unifont \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m patchright install chromium \
    && python -m patchright install chrome

COPY app/ ./app/
COPY profiles/ ./profiles/
COPY tools/ ./tools/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8012

# Start Xvfb in background then exec the Python app. Replaces `xvfb-run`
# wrapper, which hangs on its SIGUSR1 ready-signal sync under Docker and
# never exec's the wrapped command.
CMD ["/usr/local/bin/docker-entrypoint.sh"]
