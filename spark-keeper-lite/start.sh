#!/usr/bin/env bash
# ============================================================================
#  抖音续火花系统 · Linux / macOS 一键启动
#
#  做四件事：找 Python → 建数据目录 → 初始化（幂等）→ 启动服务（含调度器）。
#  首次运行会打印管理员账号与随机密码，请立刻记下并在登录后修改。
#
#  用法：
#     ./start.sh                  # 默认监听 0.0.0.0:8000
#     ./start.sh --port 9000      # 换端口
#     ./start.sh --host 127.0.0.1 # 只允许本机访问
#     ./start.sh --init-only      # 只初始化不启动
#     ./start.sh --demo           # 演示模式（不启动真实浏览器，用于评估界面）
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 定位后端目录（兼容两种目录结构：发行包与开发仓库）----
if [[ -f "$SCRIPT_DIR/backend/main.py" ]]; then
  BACKEND="$SCRIPT_DIR/backend"
elif [[ -f "$SCRIPT_DIR/main.py" ]]; then
  BACKEND="$SCRIPT_DIR"
else
  echo "[错误] 找不到 main.py，请在项目根目录运行本脚本。" >&2
  exit 2
fi

# ---- 找 Python 3.9+ ----
PYTHON=""
for candidate in "${SK_PYTHON:-}" python3.12 python3.11 python3.10 python3 python; do
  [[ -z "$candidate" ]] && continue
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")"
    major="${version%%.*}"; minor="${version##*.}"
    if [[ "$major" -eq 3 && "$minor" -ge 9 ]]; then
      PYTHON="$candidate"
      break
    fi
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "[错误] 没有找到 Python 3.9+。请先安装：" >&2
  echo "        Ubuntu/Debian: sudo apt update && sudo apt install -y python3" >&2
  echo "        CentOS/RHEL:   sudo yum install -y python3" >&2
  echo "        或者用 SK_PYTHON=/path/to/python ./start.sh 指定解释器" >&2
  exit 3
fi

echo "  使用 Python: $("$PYTHON" -V 2>&1)  ($PYTHON)"

# ---- 参数解析 ----
HOST="${SK_HOST:-0.0.0.0}"
PORT="${SK_PORT:-8000}"
INIT_ONLY=0
DEMO=0
NO_SCHEDULER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --init-only) INIT_ONLY=1; shift ;;
    --demo) DEMO=1; shift ;;
    --no-scheduler) NO_SCHEDULER=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "[警告] 未知参数：$1" >&2; shift ;;
  esac
done

export SK_HOST="$HOST"
export SK_PORT="$PORT"
export SK_DATA_DIR="${SK_DATA_DIR:-$SCRIPT_DIR/data}"
[[ "$DEMO" == "1" ]] && export SK_DEMO_MODE=true

cd "$BACKEND"

echo "  数据目录: $SK_DATA_DIR"
mkdir -p "$SK_DATA_DIR"

echo "  正在初始化（幂等，已有数据不会覆盖）……"
"$PYTHON" main.py init

if [[ "$INIT_ONLY" == "1" ]]; then
  echo "  已完成初始化（--init-only）。启动命令：cd $BACKEND && python main.py serve"
  exit 0
fi

# ---- 环境体检：只提示，不阻塞（缺浏览器时任务会失败，但站点仍可管理）----
"$PYTHON" main.py check || true

# ---- 启动 ----
echo ""
if [[ "$DEMO" == "1" ]]; then
  echo "  [演示模式] 不会启动真实浏览器，登录与任务用假数据走通，仅用于评估界面。"
fi
echo "  启动中…… 浏览器访问 http://<服务器IP>:$PORT"
echo "  停止：Ctrl+C    后台运行建议用 nohup 或 systemd（见 deploy/DEPLOY.md）"
echo ""
ARGS=(main.py serve --host "$HOST" --port "$PORT")
[[ "$NO_SCHEDULER" == "1" ]] && ARGS+=(--no-scheduler)
exec "$PYTHON" "${ARGS[@]}"
