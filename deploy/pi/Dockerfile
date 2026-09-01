# Money Printer sandbox runtime for maia (Raspberry Pi 4, linux/arm64).
# Build ON the Pi (or with buildx --platform linux/arm64):
#   docker compose -f deploy/pi/docker-compose.yml build
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    PIP_NO_CACHE_DIR=1

# tini for signal handling, curl for the container healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first for build caching
COPY deploy/pi/requirements-runtime.txt /tmp/requirements-runtime.txt
RUN pip install -r /tmp/requirements-runtime.txt

# The application. .dockerignore keeps data/, logs/, reports/ and the
# vm_snapshot out of the build context.
COPY . /app

# Non-root user. data/ and logs/ are bind-mounted in compose, but the image
# must also run bare: the logger mkdirs logs/ at import time, so both dirs
# must exist and be writable by the runtime UID.
RUN useradd --create-home --uid 1000 --shell /bin/bash mp || true \
    && mkdir -p /app/data /app/logs \
    && chown -R 1000:1000 /app/data /app/logs
USER 1000:1000

ENV PYTHONPATH=/app

ENTRYPOINT ["/usr/bin/tini", "--"]
# Bare-image default only: deploy/pi/docker-compose.yml overrides this with
# the same command using ${SIM_BALANCE:-3000}, so balance changes need no
# rebuild. Keep the two in sync.
CMD ["python", "scripts/run_web_dashboard.py", "--auto-cycle", "--sim-balance", "3000", "--no-browser", "--host", "0.0.0.0"]
