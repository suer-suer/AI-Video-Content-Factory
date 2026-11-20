# AI Video Content Factory — Web (Cloud) edition
# Build:  docker build -t ai-video-factory .
# Run:    docker run -p 8765:8765 -v factory-data:/app/pipeline/data \
#           -v factory-config:/app/pipeline/config -e WORKER_TOKEN=... ai-video-factory
# NOTE: CPU image for orchestration + cloud/API providers. Local GPU models
# (SD/whisper/TTS clones) are a Desktop-edition concern, not this container.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pipeline/ /app/pipeline/
COPY README.md /app/README.md
COPY LICENSE /app/LICENSE

RUN pip install --no-cache-dir -e /app/pipeline

# Seed the Web edition flags (a mounted volume overrides these). Without this
# file the app defaults to the Desktop edition and the API runs unauthenticated
# behind the container port (review fix).
RUN mkdir -p /app/pipeline/config && \
    printf 'edition: web\nhosted_mode: true\nregistration_open: true\nmax_concurrent_runs: 1\nworkers_enabled: false\n' \
      > /app/pipeline/config/ui_settings.yaml

ENV PYTHONUNBUFFERED=1 \
    PIPELINE_EDITION=web

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -fsS http://127.0.0.1:8765/api/health || exit 1

CMD ["python", "-c", "from ui.server import main; main(port=8765, host='0.0.0.0')"]
