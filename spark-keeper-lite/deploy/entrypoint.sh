#!/bin/sh
# =============================================================================
#  spark-keeper 容器入口脚本（POSIX sh，python:3.11-slim 里没有 bash 语法糖）
#
#  做四件事，顺序不能换：
#    1. 确保数据目录存在且可写（卷没挂好时立刻失败，而不是等迁移报错）
#    2. 等数据库可用（卷在慢速盘/网络盘上时，迁移会抢跑）
#    3. 执行迁移（幂等）
#    4. 没有任何管理员时初始化一个，并把初始密码写进 data/INITIAL_ADMIN.txt（权限 600）
#
#  为什么把初始密码写文件：`docker logs` 会被日志收集系统长期保存，
#  密码只该让部署者本人看到一次。文件落在数据卷里，容器重建也不会丢。
#
#  裸机也能用：start.sh 会设好 PYTHON=python3 再调用本脚本。
# =============================================================================
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

# 容器里有 python；很多 Linux 发行版只有 python3，由调用方覆盖
PYTHON="${PYTHON:-python}"

DATA_DIR="${SK_DATA_DIR:-$APP_DIR/data}"
ADMIN_USER="${SK_ADMIN_USER:-admin}"
INIT_FILE="$DATA_DIR/INITIAL_ADMIN.txt"
WAIT_SECONDS="${SK_DB_WAIT_SECONDS:-30}"

log() { printf '[entrypoint] %s\n' "$*"; }

# --- 1. 数据目录 ---
log "数据目录：$DATA_DIR"
mkdir -p "$DATA_DIR" "$DATA_DIR/sessions" "$DATA_DIR/screenshots" "$DATA_DIR/uploads" "$DATA_DIR/backups"
if ! touch "$DATA_DIR/.write_test" 2>/dev/null; then
    log "[错误] 数据目录不可写：$DATA_DIR"
    log "       检查 volumes 是否挂载，以及宿主机目录权限（容器内 uid=10001）"
    exit 1
fi
rm -f "$DATA_DIR/.write_test"

# --- 2. 等数据库 ---
log "等待数据库就绪（最多 ${WAIT_SECONDS}s）……"
waited=0
while :; do
    if "$PYTHON" - <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, ".")
from app import db
from app.config import get_settings
get_settings().ensure_dirs()
db.query_scalar("SELECT 1", (), 1)
PY
    then
        break
    fi
    waited=$((waited + 1))
    if [ "$waited" -ge "$WAIT_SECONDS" ]; then
        log "[错误] 等待数据库超时（${WAIT_SECONDS}s）。数据库在别的进程/网络盘上时，调大 SK_DB_WAIT_SECONDS"
        exit 1
    fi
    sleep 1
done

# --- 3. 迁移 ---
log "执行数据库迁移……"
"$PYTHON" main.py migrate

# --- 4. 无管理员则初始化 ---
if "$PYTHON" - <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, ".")
from app import db
row = db.query_one("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")
sys.exit(0 if row else 1)
PY
then
    log "管理员已存在，跳过初始化（忘记密码：$PYTHON main.py admin --password '新密码'）"
else
    log "未检测到管理员，开始初始化……"
    if [ -n "${SK_ADMIN_PASSWORD:-}" ]; then
        PASSWORD="$SK_ADMIN_PASSWORD"
    else
        # 必须保证「同时含字母与数字」（common/security.password_problem 的硬要求），
        # 末尾固定补 "8a"：否则 token_urlsafe 有约 6% 概率生成纯字母口令，
        # 首次启动会直接以「密码不合规」失败——这种只能靠运气的代码不能留在入口脚本里。
        PASSWORD="$("$PYTHON" -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(12))+"8a")')"
    fi
    SK_ADMIN_PASSWORD="$PASSWORD" "$PYTHON" main.py init --username "$ADMIN_USER"
    umask 077
    {
        printf '# spark-keeper 初始管理员（首次启动自动生成）\n'
        printf '# 登录后请立即修改密码，然后删除本文件。\n'
        printf 'username=%s\n' "$ADMIN_USER"
        printf 'password=%s\n' "$PASSWORD"
        printf 'created_at=%s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')"
    } > "$INIT_FILE"
    chmod 600 "$INIT_FILE" 2>/dev/null || true
    log "初始管理员已创建，密码写入 $INIT_FILE（权限 600）"
fi

log "启动：$*"
exec "$@"
