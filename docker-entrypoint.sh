#!/bin/sh
# 首次启动：没有 config.json 时从模板复制。
set -eu
cd /app/server
if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "[docker] 已从 config.example.json 生成 config.json，请按需填写邮箱 / 代理 / 推送"
fi
mkdir -p db/data logs
exec uv run python main.py --serve
