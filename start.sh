#!/bin/sh
# grok-rego 一键启动：检查前后端环境后拉起管理 API 与前端 Vite
# 兼容 POSIX sh（dash）与 bash

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

if [ -f server/pyproject.toml ]; then
  ok "后端目录 server/"
else
  bad "未找到 server/pyproject.toml"
fi

if [ -f web/package.json ]; then
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
  case "$node_major" in
    ''|*[!0-9]*)
      bad "无法解析 Node.js 版本: ${node_ver}"
      ;;
    *)
      if [ "$node_major" -ge 18 ]; then
        ok "Node.js v${node_ver}"
      else
        bad "Node.js 需要 >= 18，当前 v${node_ver}"
      fi
      ;;
  esac
else
  bad "未找到 Node.js >= 18，请安装: https://nodejs.org/"
fi

if command -v npm >/dev/null 2>&1; then
  ok "npm $(npm -v)"
else
  bad "未找到 npm"
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "环境检查未通过，已中止启动。"
  exit 1
fi

echo
echo "----------------------------------------"
echo "  准备依赖"
echo "----------------------------------------"
echo

if [ ! -f server/config.json ]; then
  if [ -f server/config.example.json ]; then
    cp server/config.example.json server/config.json
    ok "已从 config.example.json 生成 server/config.json"
  else
    echo "[FAIL] 缺少 server/config.json 与 config.example.json"
    exit 1
  fi
else
  ok "server/config.json"
fi

# 后端依赖：已就绪则静默校验（快），否则流式显示安装进度
if [ -d server/.venv ] && (cd server && uv sync >/dev/null 2>&1); then
  ok "后端依赖已就绪"
else
  echo "[..]   安装后端依赖（首次会下载 Python 3.13 与依赖包，进度如下）"
  echo "----------------------------------------"
  (
    cd server || exit 1
    uv sync
  ) || {
    echo
    echo "[FAIL] 后端 uv sync 失败"
    exit 1
  }
  echo "----------------------------------------"
  ok "后端依赖已就绪"
fi

if (cd server && uv run python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >/dev/null 2>&1); then
  py_ver="$(cd server && uv run python -c "import sys; print(sys.version.split()[0])" 2>/dev/null)"
  ok "Python ${py_ver}"
else
  bad "Python 需要 >= 3.13"
  exit 1
fi

if [ -d web/node_modules/vite ]; then
  ok "前端依赖已就绪"
else
  echo "[..]   安装前端依赖（npm install，进度如下）"
  echo "----------------------------------------"
  (
    cd web || exit 1
    npm install --no-fund --no-audit
  ) || {
    echo
    echo "[FAIL] npm install 失败"
    exit 1
  }
  echo "----------------------------------------"
  ok "前端依赖已就绪"
fi

echo
echo "----------------------------------------"
echo "  启动服务"
echo "----------------------------------------"
echo

mkdir -p server/logs
SERVER_LOG="server/logs/server.log"

# 可移植端口探测：优先 nc，其次 curl，最后 python3
port_open() {
  host="$1"
  port="$2"
  if command -v nc >/dev/null 2>&1; then
    nc -z "$host" "$port" >/dev/null 2>&1
  elif command -v curl >/dev/null 2>&1; then
    curl -s --max-time 1 -o /dev/null "http://${host}:${port}/"
  else
    python3 - "$host" "$port" <<'EOF' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
EOF
  fi
}

wait_port() {
  host="$1"
  port="$2"
  pid="$3"
  timeout="${4:-30}"
  n=0
  max=$((timeout * 5))
  printf "[..]   等待 %s:%s 就绪 " "$host" "$port"
  while [ "$n" -lt "$max" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf "x\n"
      return 1
    fi
    if port_open "$host" "$port"; then
      printf "ok\n"
      return 0
    fi
    sleep 0.2
    n=$((n + 1))
    if [ $((n % 5)) -eq 0 ]; then
      printf "."
    fi
  done
  printf "超时\n"
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
