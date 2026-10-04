# grok-rego：管理 API + Web UI + Camoufox，可跑批量注册。
# 仅 linux/amd64。构建时在线安装 Camoufox 内核。
#   docker compose up --build
FROM node:22-bookworm-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm
WORKDIR /app/server
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libxshmfence1 \
        libxtst6 \
    && rm -rf /var/lib/apt/lists/*

COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server/ ./
COPY --from=web /src/web/dist ./web-dist

RUN mkdir -p /app/server/db/data /app/server/logs \
    && uv run camoufox fetch

ENV GROK_REGO_HOST=0.0.0.0 \
    GROK_REGO_PORT=8787 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy
EXPOSE 8787

CMD ["sh", "-c", "mkdir -p /app/server/db/data /app/server/logs && exec uv run python main.py --serve"]
