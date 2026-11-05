# Radar de Passages: one container = web UI/API + background scheduler (single instance, SQLite on a volume).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RADAR_ENV=production \
    RADAR_SCHEDULER=1 \
    RADAR_DB=/data/radar.sqlite3 \
    RADAR_HOST=0.0.0.0 \
    PORT=8003

WORKDIR /app

# Exact, verified dependency closure first so this layer is cached.
COPY requirements-lock.txt ./
RUN pip install -r requirements-lock.txt

# Optional: a headless Chromium for stores whose prices only appear after JavaScript runs
# (RADAR_RENDER_DOMAINS). Off by default: it adds several hundred MB and wants >= 1 GB of RAM.
#   docker build --build-arg WITH_BROWSER=1 .
ARG WITH_BROWSER=0
ARG PLAYWRIGHT_VERSION=1.63.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$WITH_BROWSER" = "1" ]; then \
      pip install "playwright==${PLAYWRIGHT_VERSION}" \
      && playwright install --with-deps chromium \
      && chmod -R a+rX /opt/ms-playwright; \
    fi

# The compiled UI (static/app.js) is committed; CI fails if it drifts from frontend/app.ts.
# sources/ is the shipped catalogue of stores, airlines, bus companies and agencies.
COPY radar ./radar
COPY sources ./sources
COPY static ./static
COPY demo ./demo
COPY run.py ./
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN groupadd --gid 10001 radar \
 && useradd --uid 10001 --gid radar --no-create-home --shell /usr/sbin/nologin radar \
 && mkdir -p /data \
 && chown radar:radar /data \
 && chmod 0755 /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8003'), timeout=4)"

# Starts as root only to fix ownership of a freshly mounted volume, then drops to uid 10001.
# Run maintenance commands as that user too:  docker compose exec -u radar radar python -m radar.main --status
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "run.py"]
