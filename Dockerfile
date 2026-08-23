# syntax=docker/dockerfile:1

# ---------- build ----------------------------------------------------------
# Dependencies are resolved in a separate stage so build tools and caches do
# not reach the runtime image.
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY requirements.lock ./

# Install into a self-contained virtualenv that the runtime stage copies whole.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install --requirement requirements.lock

# ---------- runtime --------------------------------------------------------
FROM python:3.12-slim AS runtime

# tzdata: the fetch boundary resolves the exchange's local date via zoneinfo.
# Without it ZoneInfo raises and the boundary silently falls back to UTC,
# which is a full day off for ASX.
# ca-certificates: HTTPS to the price provider and the symbol directory.
RUN apt-get update && \
    apt-get install --no-install-recommends -y tzdata ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Which commit this image was built from. The image excludes .git, so the
# manifest cannot ask git; without this every run records "unknown".
#   docker build --build-arg GIT_REVISION=$(git rev-parse --short HEAD) .
ARG GIT_REVISION=unknown

ENV STOCKS_CODE_REVISION=${GIT_REVISION} \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Data lives on a mounted volume, never in the image layer.
    STOCKS_DATA_ROOT=/data

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
# src and config only: config/*.csv are the bundled universe seeds, copied to
# the data volume on first run.
COPY src/ ./src/
COPY config/ ./config/

# Non-root. The volume must be writable by this uid; compose and the ECS task
# definition both set it.
RUN useradd --create-home --uid 10001 stocks && \
    mkdir -p /data && chown -R stocks:stocks /data /app
USER stocks

# Fails if the package set or config is broken, without touching the network.
HEALTHCHECK --interval=1h --timeout=10s --retries=1 \
    CMD ["python", "-c", "import sys; sys.path.insert(0,'/app/src'); import analysis, pipeline, universe"]

ENTRYPOINT ["python", "/app/src/run.py"]
CMD ["--help"]
