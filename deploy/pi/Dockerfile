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

# Non-root user; data/ and logs/ are volume-mounted and chowned by compose
RUN useradd --create-home --uid 1000 --shell /bin/bash mp || true
USER 1000:1000

ENV PYTHONPATH=/app

ENTRYPOINT ["/usr/bin/tini", "--"]
# Feed-only harvester + dashboard, same workload the VM ran 24/7.
# Swap the command to scripts/run_sandbox.py when a strategy is cleared for
# paper trading (see deploy/pi/docker-compose.yml comments).
CMD ["python", "scripts/run_web_dashboard.py", "--auto-cycle", "--sim-balance", "3000", "--no-browser", "--host", "0.0.0.0"]
