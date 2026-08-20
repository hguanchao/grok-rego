#!/usr/bin/env bash
# grok-rego 一键启动：检查前后端环境后拉起管理 API 与前端 Vite

set -u
cd "$(dirname "$0")" || exit 1

fail=0

ok()  { printf '[OK]   %s\n' "$1"; }
bad() { printf '[FAIL] %s\n' "$1"; fail=1; }

echo
echo "========================================"
echo "  grok-rego 环境检查"
echo "========================================"
echo

if [[ -f server/pyproject.toml ]]; then
  ok "后端目录 server/"
else
  bad "未找到 server/pyproject.toml"
fi

if [[ -f web/package.json ]]; then
  ok "前端目录 web/"
else
  bad "未找到 web/package.json"
fi

if command -v uv >/dev/null 2>&1; then
  ok "$(uv --version)"
else
  bad "未找到 uv，请安装: https://docs.astral.sh/uv/"
fi

if command -v node >/dev/null 2>&1; then
  node_ver="$(node -v)"
  node_ver="${node_ver#v}"
  node_major="${node_ver%%.*}"
  if [[ "$node_major" =~ ^[0-9]+$ ]] && (( node_major >= 18 )); then
    ok "Node.js v${node_ver}"
  else
    bad "Node.js 需要 >= 18，当前 v${node_ver}"
  fi
else
  bad "未找到 Node.js >= 18，请安装: https://nodejs.org/"
fi

if command -v npm >/dev/null 2>&1; then
  ok "npm $(npm -v)"
else
  bad "未找到 npm"
fi

if (( fail != 0 )); then
  echo
  echo "环境检查未通过，已中止启动。"
  exit 1
fi

echo
echo "----------------------------------------"
echo "  准备依赖"
echo "----------------------------------------"
echo

if [[ ! -f server/config.json ]]; then
  if [[ -f server/config.example.json ]]; then
    cp server/config.example.json server/config.json
    ok "已从 config.example.json 生成 server/config.json"
  else
    echo "[FAIL] 缺少 server/config.json 与 config.example.json"
    exit 1
  fi
else
  ok "server/config.json"
fi

(
  cd server || exit 1
  uv sync >/dev/null 2>&1 || exit 1
  uv run python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >/dev/null 2>&1 || {
    echo "[FAIL] Python 需要 >= 3.13"
    exit 1
  }
  py_ver="$(uv run python -c "import sys; print(sys.version.split()[0])" 2>/dev/null)"
  echo "[OK]   Python ${py_ver}"
) || {
  echo "[FAIL] 后端 uv sync 失败"
  exit 1
}
ok "后端依赖已就绪"

if [[ ! -d web/node_modules/vite ]]; then
  echo "[..]   前端依赖缺失，执行 npm install"
  (
    cd web || exit 1
    npm install >/dev/null 2>&1
  ) || {
    echo "[FAIL] npm install 失败"
    exit 1
  }
fi
ok "前端依赖已就绪"

echo
echo "----------------------------------------"
echo "  启动服务"
echo "----------------------------------------"
echo

mkdir -p server/logs
SERVER_LOG="server/logs/server.log"

wait_port() {
  local host="$1" port="$2" pid="$3" timeout="${4:-30}"
  local n=0
  local max=$((timeout * 5))
  while (( n < max )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    if (echo >/dev/tcp/"$host"/"$port") >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
    n=$((n + 1))
  done
  return 1
}

# 1) 先启动后端（stdout/stderr 写入 server.log，不在控制台打印）
(
  cd server || exit 1
  exec uv run python main.py --serve 8787
) >>"$SERVER_LOG" 2>&1 &
server_pid=$!
if ! wait_port 127.0.0.1 8787 "$server_pid" 30; then
  echo "[FAIL] 后端未在 30s 内就绪 (http://127.0.0.1:8787)，详见 server/logs/server.log"
  exit 1
fi
echo "[OK]   后端服务  http://127.0.0.1:8787  (pid ${server_pid})"

# 2) 再启动前端（输出直接打印到控制台，不生成 web 日志文件）
(
  cd web || exit 1
  exec npm run --silent dev
) &
web_pid=$!
if ! wait_port 127.0.0.1 5274 "$web_pid" 30; then
  echo "[FAIL] 前端未在 30s 内就绪 (http://127.0.0.1:5274)，请查看上方控制台输出"
  exit 1
fi
echo "[OK]   前端服务  http://127.0.0.1:5274  (pid ${web_pid})"

cleanup() {
  trap - INT TERM EXIT
  kill "$server_pid" "$web_pid" 2>/dev/null || true
  wait "$server_pid" "$web_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo
echo "前后端已在后台运行，日志见 server/logs/。Ctrl+C 同时停止。"
echo

wait
