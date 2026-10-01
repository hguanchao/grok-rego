# 完整镜像：精简能力 + Camoufox 无头浏览器，可跑批量注册。
# 仅 linux/amd64。构建时优先用仓库根目录的 camoufox-*-lin.x86_64.zip 离线装内核。
#   docker compose --profile full up --build
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
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

# 可选离线内核：不存在时 COPY 出空目录，启动脚本再决定 fetch 还是 local。
COPY camoufox-*-lin.x86_64.zip* /tmp/camoufox/
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/server/db/data /app/server/logs \
    && KERNEL="$(find /tmp/camoufox -maxdepth 1 -type f -name 'camoufox-*-lin.x86_64.zip' | head -n 1 || true)" \
    && if [ -n "$KERNEL" ]; then \
         echo "[docker] 离线安装 Camoufox: $KERNEL"; \
         uv run python fetch_camoufox_local.py "$KERNEL"; \
       else \
         echo "[docker] 未找到离线 zip，构建时在线 fetch Camoufox 内核"; \
         uv run camoufox fetch; \
       fi \
    && rm -rf /tmp/camoufox

ENV GROK_REGO_HOST=0.0.0.0 \
    GROK_REGO_PORT=8787 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy
EXPOSE 8787
ENTRYPOINT ["/app/docker-entrypoint.sh"]
