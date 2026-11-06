# Stage 1: Compile TypeScript + React 19 PWA static assets
FROM node:22-slim AS web-builder
WORKDIR /web
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Lightweight glibc Python runtime (native ARM64 Raspberry Pi + AMD64 wheels)
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml ./
COPY backend/ ./backend/
RUN uv sync --no-dev

COPY --from=web-builder /web/dist ./backend/mesoscale/static_dist

RUN useradd -m -u 1000 mesoscale && mkdir -p /data && chown -R mesoscale:mesoscale /app /data
USER mesoscale

ENV MESOSCALE_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["uv", "run", "uvicorn", "mesoscale.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
