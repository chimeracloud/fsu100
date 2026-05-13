# Track 1 production lay engine — single Cloud Run service.
# Build context: this directory (track1/).
#
#   docker build -t fsu100-track1 .
#   docker run -p 8080:8080 fsu100-track1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Strategy modules (copied verbatim from charles-ascot/lay-engine).
COPY rules.py signal_filters.py top2_concentration.py market_overlay.py ./

# Engine layer.
COPY settings.py models.py evaluator.py engine.py gcs.py main.py ./

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail http://localhost:${PORT}/health || exit 1

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1
