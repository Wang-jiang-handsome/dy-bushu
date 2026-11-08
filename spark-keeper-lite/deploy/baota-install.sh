#!/usr/bin/env bash
# =============================================================================
#  spark-keeper · 宝塔 / 通用 Linux 一键部署脚本（路线 B：不用 Docker）
#
#  它做什么（每一步都幂等，可以重复执行）：
#    1. 校验环境：Python 3.9+、项目结构、systemd、端口占用
#    2. 安装 Chromium + 中文字体 + tzdata（Debian/Ubuntu、RHEL/CentOS 系）
#    3. 建 data/ 目录骨架，并把属主改成运行用户
#    4. 生成/补全 .env（只补缺失项，绝不覆盖你已有的值）
#    5. 从 deploy/spark-keeper.service 生成 systemd 单元（替换路径/用户/端口）
#    6. main.py init（建库 + 首次管理员），然后 enable --now 启动
#    7. 轮询 /api/v1/health 确认真起来了，并打印后续 Nginx 反代配置
#
#  用法（宝塔终端里，root 执行）：
#     bash /www/wwwroot/spark-keeper/deploy/baota-install.sh --check     # 只体检
#     bash /www/wwwroot/spark-keeper/deploy/baota-install.sh --dry-run   # 只打印计划
#     bash /www/wwwroot/spark-keeper/deploy/baota-install.sh             # 真装
#     bash /www/wwwroot/spark-keeper/deploy/baota-install.sh --uninstall # 卸载（保留 data）
#
#  可用参数：
#     --dir PATH        项目根目录（默认：本脚本上一级目录）
#     --user NAME       运行用户（默认：宝塔的 www；不存在则用当前用户）
#     --port N          监听端口（默认 8000，只监听 127.0.0.1，由 Nginx 反代）
#     --python PATH     指定 Python 3.9+ 解释器（默认自动找 python3.13…3.9、宝塔 Python 管理器、uv）
#     --skip-packages   跳过 apt/yum 安装（你自己装好 chromium 时用）
#     --dry-run         只打印将要执行的操作，不落盘不启动
#     --check           只做环境体检，返回非 0 表示有问题
#     --uninstall       停服务 + 删 unit（data/ 保留）
#     -h | --help       显示帮助
#
#  注意：CentOS / RHEL 7（glibc 2.17）**不能原生部署** —— 系统 Python 只有 3.6，
#  且装不了能用的新版 Chromium。这类机器请用 Docker 部署（见 DEPLOY_PLATFORMS.md §3.5）。
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="spark-keeper"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

PORT=8000
SVC_USER=""
SVC_GROUP=""
SKIP_PACKAGES=0
DRY_RUN=0
CHECK_ONLY=0
UNINSTALL=0
ERRORS=0
#: 实际使用的 Python 解释器（>=3.9）。由 resolve_python 决定，可能是
#: /usr/bin/python3.11、宝塔装的那个、或 uv 装的，不一定是 `python3`。
PY=""
#: 1 = Python 带 _ssl（OpenSSL）。缺了它登录/鉴权接口根本不会注册。
SSL_OK=1
#: 1 = 系统是老版 CentOS/RHEL 7（glibc 2.17）：装不了新版 Chromium，只能走 Docker 或换系统
IS_EL7=0

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_OK="$(tput setaf 2)"; C_WARN="$(tput setaf 3)"; C_ERR="$(tput setaf 1)"
  C_DIM="$(tput setaf 8)"; C_OFF="$(tput sgr0)"
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

log()  { printf '%s[✓]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
info() { printf '%s[·]%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
err()  { printf '%s[✗]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; ERRORS=$((ERRORS + 1)); }
die()  { trap - ERR; err "$*"; printf '\n%s安装中止。%s\n' "$C_ERR" "$C_OFF" >&2; exit 1; }

# 出错时打印「哪条命令失败」——比行号有用（subshell/函数里 LINENO 常指向第 1 行）
trap 'ec=$?; if [ $ec -ne 0 ]; then printf "%s失败：%s（退出码 %s）%s\n" "$C_ERR" "$BASH_COMMAND" "$ec" "$C_OFF" >&2; fi' ERR

usage() {
  # 打印脚本头部注释（从第 2 行到第一行非注释为止），避免把代码也打出来
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)            PROJECT_ROOT="${2:?--dir 需要参数}"; shift 2 ;;
    --user)           SVC_USER="${2:?--user 需要参数}"; shift 2 ;;
    --port)           PORT="${2:?--port 需要参数}"; shift 2 ;;
    --python)         PY="${2:?--python 需要参数}"; shift 2 ;;
    --skip-packages)  SKIP_PACKAGES=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --check)          CHECK_ONLY=1; shift ;;
    --uninstall)      UNINSTALL=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

PROJECT_ROOT="$(cd "$PROJECT_ROOT" 2>/dev/null && pwd)" || die "目录不存在：$PROJECT_ROOT"
DATA_DIR="$PROJECT_ROOT/data"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_TEMPLATE="$PROJECT_ROOT/deploy/.env.example"
UNIT_TEMPLATE="$PROJECT_ROOT/deploy/spark-keeper.service"
MAIN_PY="$PROJECT_ROOT/main.py"

# 真正的执行器：dry-run 只打印
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s  [dry-run] %s%s\n' "$C_DIM" "$*" "$C_OFF"
    return 0
  fi
  "$@"
}

need_root() {
  [ "$(id -u)" -eq 0 ] || die "需要 root 执行（宝塔终端默认就是 root）：sudo bash $0 $*"
}

# ---------------------------------------------------------------------------
# 系统识别
# ---------------------------------------------------------------------------
OS_ID=""; OS_LIKE=""; OS_VER=""; PKG=""; CHROME_PKG=""; FONT_PKG=""

detect_os() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_LIKE="${ID_LIKE:-}"
    OS_VER="${VERSION_ID:-}"
  fi
  local key="$OS_ID $OS_LIKE"
  case "$key" in
    *debian*|*ubuntu*)
      PKG="apt"; CHROME_PKG="chromium"; FONT_PKG="fonts-noto-cjk" ;;
    *rhel*|*centos*|*fedora*|*rocky*|*almalinux*|*tencentos*|*opencloudos*|*anolis*)
      PKG="yum"; CHROME_PKG="chromium"; FONT_PKG="google-noto-sans-cjk-fonts" ;;
    *alpine*)
      PKG="apk"; CHROME_PKG="chromium"; FONT_PKG="font-noto-cjk" ;;
    *)
      PKG=""; CHROME_PKG=""; FONT_PKG="" ;;
  esac

  # CentOS / RHEL 7：2024-06 已 EOL，两个硬伤会让「原生部署」走不通：
  #   1) glibc 2.17 —— 新版 Chromium 从 114 起要求 glibc >= 2.28，EPEL7 里的
  #      那个老版本 chromium 已经跟不上抖音前端；
  #   2) 官方 yum 源已下线（mirrorlist 404），装任何包都会失败。
  # 所以这里明确标记，后续只给 Docker / 换系统两条路，不再尝试 yum install。
  case "$OS_ID:$OS_VER" in
    centos:7*|rhel:7*|"centos:7.9"*)
      IS_EL7=1 ;;
  esac
  if [ "$IS_EL7" -eq 1 ] || [ "$OS_ID" = "centos" ] && [ "${OS_VER%%.*}" = "7" ]; then
    IS_EL7=1
  fi

  if [ -z "$PKG" ]; then
    warn "未能识别发行版（ID=${OS_ID:-未知}）：跳过包安装，请自行确保已装 chromium 与中文字体"
  else
    info "系统：${PRETTY_NAME:-$OS_ID}（包管理器 $PKG）"
  fi
  if [ "$IS_EL7" -eq 1 ]; then
    warn "检测到 CentOS/RHEL 7（已 EOL，glibc 2.17）：本机装不了能用的 Chromium"
    warn "  → 请用 Docker 部署（见 deploy/DEPLOY_PLATFORMS.md §3.5 路线 A），或换 Ubuntu 22.04 / Rocky 9"
  fi
}

pkg_install() {
  [ "$SKIP_PACKAGES" -eq 1 ] && { info "--skip-packages：跳过安装 $*"; return 0; }
  [ -z "$PKG" ] && { warn "无包管理器，跳过安装：$*"; return 0; }
  if [ "$IS_EL7" -eq 1 ]; then
    warn "CentOS 7 源已失效，跳过 yum 安装：$*"
    return 0
  fi
  case "$PKG" in
    apt) run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" ;;
    yum) run yum install -y "$@" ;;
    apk) run apk add --no-cache "$@" ;;
  esac
}

# ---------------------------------------------------------------------------
# 1) 环境检查
# ---------------------------------------------------------------------------
resolve_user() {
  if [ -n "$SVC_USER" ]; then
    id "$SVC_USER" >/dev/null 2>&1 || die "用户不存在：$SVC_USER"
  elif id www >/dev/null 2>&1; then
    SVC_USER="www"                      # 宝塔的站点用户
  elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    SVC_USER="$SUDO_USER"
  else
    SVC_USER="root"
    warn "没找到 www 用户，将以 root 运行（能用，但不如普通用户安全）"
  fi
  SVC_GROUP="$(id -gn "$SVC_USER" 2>/dev/null || echo "$SVC_USER")"
  log "运行用户：$SVC_USER:$SVC_GROUP"
}

py_version_of() {
  # 输出 "3.11" 之类的版本号；不可执行/是占位程序时输出空
  "$1" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true
}

py_version_ok() {
  # >= 3.9 返回 0
  local ver="$1" major minor
  [ -n "$ver" ] || return 1
  major="${ver%%.*}"; minor="${ver##*.}"
  [ "$major" -gt 3 ] && return 0
  [ "$major" -eq 3 ] && [ "$minor" -ge 9 ] && return 0
  return 1
}

#: 把所有「可能装了 3.9+」的位置列出来（宝塔的 Python 管理器、uv、系统多版本）
python_candidates() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3 \
           /usr/local/bin/python3.11 /usr/local/bin/python3.12 \
           /opt/python/bin/python3 /www/server/python_manager/versions/*/bin/python3 \
           "$HOME/.local/bin/python3.11" "$HOME/.local/share/uv/python"/*/bin/python3
  do
    # 用 `|| continue` 而不是 `&& printf`：后者在 set -E 下会让 ERR trap
    # 把「某个候选不存在」当成真失败打出来，噪音很大
    command -v "$c" >/dev/null 2>&1 || continue
    printf '%s\n' "$c"
  done
}

resolve_python() {
  local cand ver
  # 1) 用户显式指定
  if [ -n "$PY" ]; then
    [ -x "$PY" ] || command -v "$PY" >/dev/null 2>&1 || die "--python 指向的解释器不存在：$PY"
    ver="$(py_version_of "$PY")"
    py_version_ok "$ver" || die "--python 指定的版本是 ${ver:-无法执行}，需要 3.9+：$PY"
    log "Python（--python 指定）：$PY（$ver）"
    return 0
  fi

  # 2) 自动按版本从高到低找
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    ver="$(py_version_of "$cand")"
    if py_version_ok "$ver"; then
      PY="$cand"
      log "Python：$PY（$ver）"
      return 0
    fi
  done <<EOF
$(python_candidates)
EOF

  # 3) 一个都不合格：按系统给出可执行的下一步，而不是干巴巴报错
  ver="$(py_version_of python3 2>/dev/null || true)"
  err "没有找到 Python 3.9+（系统 python3 是 ${ver:-未知}）"
  printf '\n%s怎么办（按这条路走下去即可）：%s\n' "$C_WARN" "$C_OFF" >&2
  if [ "$IS_EL7" -eq 1 ]; then
    cat >&2 <<EOF
  CentOS/RHEL 7 上「原生部署」有两个死结，建议直接用 Docker（镜像内自带 Python 3.11 + Chromium）：

    # 1) 装 Docker（宝塔「Docker」菜单也能装；命令行等价如下）
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker

    # 2) 起服务（不需要你本机有 Python 3.9 或 chromium）
    cd $PROJECT_ROOT
    cp deploy/.env.example .env && vi .env      # 改 SK_SECRET / SK_PUBLIC_URL
    docker compose up -d --build
    docker compose logs -f app

  然后宝塔建站点 → 反向代理 → http://127.0.0.1:8000（详见 deploy/DEPLOY_PLATFORMS.md §3.5 路线 A）。
  长期建议：CentOS 7 已 EOL（无安全更新），换 Ubuntu 22.04 / Rocky 9 一劳永逸。
EOF
  else
    cat >&2 <<EOF
  任选一种装 Python 3.9+，然后用 --python 指给本脚本：

    # A. 用 uv（最快，静态二进制，不需要编译）
    curl -LsSf https://astral.sh/uv/install.sh | sh
    uv python install 3.11
    bash deploy/baota-install.sh --python "\$(uv python find 3.11)"

    # B. 用宝塔「软件商店 → Python 项目管理器」装 3.11，然后
    bash deploy/baota-install.sh --python /www/server/python_manager/versions/3.11*/bin/python3

    # C. 换 Docker 部署（本机什么都不用装）
    docker compose up -d --build
EOF
  fi
  printf '\n' >&2
  die "请先解决 Python 版本问题"
}

check_layout() {
  [ -f "$MAIN_PY" ] || die "这里不像项目根目录（找不到 main.py）：$PROJECT_ROOT
  提示：脚本应放在 <项目>/deploy/ 下；如果你把代码解压到了别处，用 --dir 指定"
  [ -d "$PROJECT_ROOT/app" ] || die "缺少 app/ 目录：$PROJECT_ROOT"
  [ -d "$PROJECT_ROOT/web/static" ] || die "缺少 web/static/ 目录（前端静态文件）：$PROJECT_ROOT"
  [ -f "$UNIT_TEMPLATE" ] || die "缺少 systemd 模板：$UNIT_TEMPLATE"
  log "项目结构正常：$PROJECT_ROOT"
}

#: 端口被谁占着？输出 "pid<TAB>命令行"。拿不到就输出空。
port_owner() {
  local pid="" line
  if command -v ss >/dev/null 2>&1; then
    line="$(ss -ltnp 2>/dev/null | awk -v want=":$PORT" \
      'substr($4, length($4)-length(want)+1) == want {print; exit}')"
    pid="$(printf '%s' "$line" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n1)"
  fi
  if [ -z "$pid" ] && command -v lsof >/dev/null 2>&1; then
    pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n1)"
  fi
  if [ -z "$pid" ] && command -v netstat >/dev/null 2>&1; then
    pid="$(netstat -ltnp 2>/dev/null | awk -v want=":$PORT" \
      'substr($4, length($4)-length(want)+1) == want {print}' \
      | sed -n 's/.*[[:space:]]\([0-9][0-9]*\)\/.*/\1/p' | head -n1)"
  fi
  [ -n "$pid" ] || return 1
  printf '%s\t%s' "$pid" "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-120)"
}

port_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk -v want=":$PORT" \
      'substr($4, length($4)-length(want)+1) == want {found=1} END {exit !found}'
    return $?
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"
    return $?
  fi
  return 1
}

#: 本项目的 systemd 服务是否正在运行
service_active() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

service_main_pid() {
  command -v systemctl >/dev/null 2>&1 || return 1
  local pid
  pid="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null)"
  [ -n "$pid" ] && [ "$pid" != "0" ] && printf '%s' "$pid"
}

check_port() {
  if ! port_listening; then
    info "端口 $PORT 空闲"
    return 0
  fi

  local owner owner_pid owner_cmd main_pid
  owner="$(port_owner || true)"
  owner_pid="${owner%%$'\t'*}"
  owner_cmd="${owner#*$'\t'}"
  main_pid="$(service_main_pid || true)"

  # 情况一：端口正被**本项目的 spark-keeper 服务**占用 —— 这是正常状态（已经在跑），
  # 不该判成「冲突」。真实事故：服务装好后重跑 --check，误报「端口 8000 已被占用」。
  if service_active && [ -n "$owner_pid" ] && { [ "$owner_pid" = "$main_pid" ] || service_owns_pid "$owner_pid"; }; then
    log "端口 $PORT 正被本项目的 spark-keeper 服务使用（已在运行，属正常）"
    info "  想换端口：bash deploy/baota-install.sh --port 8100（会重写 unit 并重启）"
    return 0
  fi

  # 情况一（二分）：ss/netstat 拿不到 pid，但 unit 是 active 且端口有人监听 —— 也按「自己」处理
  if service_active && [ -z "$owner_pid" ]; then
    log "端口 $PORT 已有服务在监听，且 spark-keeper 处于 active（按本项目自身处理）"
    return 0
  fi

  # 情况二：被别的进程占了 —— 明确告诉用户是谁、怎么让开
  err "端口 $PORT 已被其它进程占用：${owner_cmd:-未知进程}"
  if [ -n "$owner_pid" ]; then
    info "  PID：$owner_pid"
    info "  看是谁：ps -fp $owner_pid"
    info "  停掉它：systemctl stop <它的服务名>   或   kill $owner_pid"
  fi
  info "  或者换端口：bash deploy/baota-install.sh --port 8100"
  info "  ⚠️ 换端口后，宝塔「反向代理」的目标 URL 也要同步改成 http://127.0.0.1:8100"
  return 1
}

#: pid 是否属于 spark-keeper.service 的 cgroup（兼容主进程 fork 的情况）
service_owns_pid() {
  local pid="$1" cgroup
  cgroup="$(cat "/proc/$pid/cgroup" 2>/dev/null | tr '\n' ' ')"
  case "$cgroup" in
    *"$SERVICE_NAME"*) return 0 ;;
  esac
  return 1
}

find_chromium() {
  local p
  for p in \
    "${SK_CHROME_PATH:-}" \
    /usr/bin/chromium \
    /usr/bin/chromium-browser \
    /usr/lib/chromium/chromium \
    /snap/bin/chromium \
    /usr/bin/google-chrome \
    /usr/bin/google-chrome-stable \
    /opt/google/chrome/chrome
  do
    [ -n "$p" ] && [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  command -v chromium chromium-browser google-chrome google-chrome-stable 2>/dev/null | head -n1
}

check_chromium() {
  local bin
  bin="$(find_chromium || true)"
  if [ -n "$bin" ]; then
    log "Chromium：$bin"
  else
    warn "没找到 Chromium（任务执行时会失败）：装完再跑一次本脚本，或手动设 SK_CHROME_PATH"
  fi
}

check_ssl() {
  # **最致命的一项**：Python 编译时没带 OpenSSL → 没有 _ssl 模块 →
  # auth / admin / user 这些接口模块整块注册失败 → 登录页只显示「接口不存在」。
  # 真实事故：CentOS 7 上用宝塔装的 Python 就是这样。
  local out code=0
  out="$("$PY" -c 'import ssl;print(ssl.OPENSSL_VERSION)' 2>&1)" || code=$?
  if [ "$code" -eq 0 ] && [ -n "$out" ]; then
    log "OpenSSL：$out（_ssl 可用）"
    return 0
  fi
  err "当前 Python 缺少 _ssl 模块（编译时没有 OpenSSL）——登录/鉴权接口会全部不可用"
  info "  解释器：$PY"
  info "  现象：网页登录页报「接口不存在」，日志里是"
  info "        [bootstrap] 接口模块 app.api.auth 依赖缺失：_ssl"
  printf '\n%s修法（任选其一）：%s\n' "$C_WARN" "$C_OFF"
  cat >&2 <<EOF
  1) 换一个自带 OpenSSL 的 Python（最快，不用编译）：
       curl -LsSf https://astral.sh/uv/install.sh | sh
       uv python install 3.11
       bash deploy/baota-install.sh --python "\$(uv python find 3.11)"

  2) 装 openssl-devel 后重装/重编 Python（宝塔「Python 项目管理器」卸载再装）：
       yum install -y openssl-devel libffi-devel bzip2-devel

  3) 改用 Docker 部署（镜像自带完整 OpenSSL，最省心）：
       docker compose up -d --build
EOF
  printf '\n' >&2
  return 1
}

check_sqlite() {
  # SQLite 版本决定能不能用「部分索引」（3.8+）和 UPSERT（3.24+）。
  # 真实事故：CentOS 7 的系统 SQLite 是 3.7.17，即使你装了 Python 3.11，
  # 只要它是链到系统 libsqlite3 编译的，建库仍会报 near "WHERE": syntax error。
  # 现在代码已兼容老版本（自动跳过），这里只做提示，让你心里有数。
  local ver
  ver="$("$PY" -c 'import sqlite3;print(sqlite3.sqlite_version)' 2>/dev/null || true)"
  [ -n "$ver" ] || { warn "读不到 SQLite 版本（不影响启动）"; return 0; }
  local major minor
  major="${ver%%.*}"; minor="$(printf '%s' "$ver" | cut -d. -f2)"
  if [ "${major:-0}" -gt 3 ] || { [ "${major:-0}" -eq 3 ] && [ "${minor:-0}" -ge 24 ]; }; then
    log "SQLite：$ver（支持部分索引与 UPSERT）"
  elif [ "${major:-0}" -eq 3 ] && [ "${minor:-0}" -ge 8 ]; then
    log "SQLite：$ver（支持部分索引；UPSERT 走降级路径）"
  else
    warn "SQLite：$ver（偏老）——已自动降级：跳过部分索引、心跳用 UPDATE→INSERT"
    warn "  当日幂等仍由调度器保证，功能不受影响；想用原生 UPSERT 需 SQLite 3.24+"
  fi
}

# ---------------------------------------------------------------------------
# 2) 安装依赖
# ---------------------------------------------------------------------------
install_packages() {
  [ "$SKIP_PACKAGES" -eq 1 ] && { info "--skip-packages：跳过系统包安装"; return 0; }
  [ -z "$PKG" ] && return 0
  info "更新软件源索引（可能比较慢，属于正常）"
  case "$PKG" in
    apt) run apt-get update -qq || warn "apt-get update 有报错，继续尝试安装" ;;
    yum) run yum makecache -q || warn "yum makecache 有报错，继续尝试安装" ;;
    apk) run apk update || true ;;
  esac
  if [ "$PKG" = "yum" ]; then
    run yum install -y epel-release || warn "安装 epel-release 失败（可能已装）"
  fi
  pkg_install tzdata ca-certificates curl
  # 中文字体：没有它截图里中文全是方块，线上排障等于瞎
  pkg_install "$FONT_PKG" || warn "中文字体安装失败（截图会显示方块，不影响发送）"
  if [ -z "$(find_chromium || true)" ]; then
    pkg_install "$CHROME_PKG" || warn "Chromium 安装失败：请手动安装后重跑，或设 SK_CHROME_PATH"
  else
    info "已有 Chromium，跳过安装"
  fi
}

# ---------------------------------------------------------------------------
# 3) 目录与权限
# ---------------------------------------------------------------------------
prepare_dirs() {
  local sub
  for sub in sessions screenshots uploads backups profiles; do
    run mkdir -p "$DATA_DIR/$sub"
    [ -f "$DATA_DIR/$sub/.gitkeep" ] || run touch "$DATA_DIR/$sub/.gitkeep"
  done
  log "数据目录就绪：$DATA_DIR"
  if [ "$(id -u)" -eq 0 ] && [ "$SVC_USER" != "root" ]; then
    run chown -R "$SVC_USER:$SVC_GROUP" "$DATA_DIR"
    run chmod -R u+rwX "$DATA_DIR"
    log "已把 data/ 属主改成 $SVC_USER:$SVC_GROUP"
  fi
}

# ---------------------------------------------------------------------------
# 4) .env（只补缺失，不覆盖）
# ---------------------------------------------------------------------------
env_has() { [ -f "$ENV_FILE" ] && grep -qE "^[[:space:]]*$1=" "$ENV_FILE"; }
env_value() { [ -f "$ENV_FILE" ] && sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n1; }

env_ensure() {
  local key="$1" value="$2" cur
  if ! env_has "$key"; then
    if [ "$DRY_RUN" -eq 1 ]; then
      info "[dry-run] 追加 $key=$value 到 .env"
      return 0
    fi
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    log "已补 $key=$value"
    return 0
  fi
  cur="$(env_value "$key")"
  if [ -z "$cur" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      info "[dry-run] 填充空的 $key=$value"
      return 0
    fi
    # 空值才补：用临时文件替换，避免 sed -i 在不同发行版上的差异
    awk -v k="$key" -v v="$value" \
      'BEGIN{done=0} { if (!done && $0 ~ "^[[:space:]]*"k"=" ) { print k"="v; done=1 } else print }' \
      "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    log "已填充空的 $key=$value"
  fi
}

#: 把某个 key 强制改成指定值（用于「模板里的 Docker 默认值在裸机上一定错」的情况）
env_force() {
  local key="$1" value="$2" cur
  cur="$(env_value "$key" || true)"
  [ "$cur" = "$value" ] && return 0
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ -z "$cur" ]; then
      info "[dry-run] 设置 $key=$value（当前为空）"
    else
      info "[dry-run] 把 $key 从「$cur」改成「$value」"
    fi
    return 0
  fi
  if env_has "$key"; then
    awk -v k="$key" -v v="$value" \
      'BEGIN{done=0} { if (!done && $0 ~ "^[[:space:]]*"k"=" ) { print k"="v; done=1 } else print }' \
      "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  log "已修正 $key=$value"
}

write_env() {
  if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_TEMPLATE" ]; then
      run cp "$ENV_TEMPLATE" "$ENV_FILE"
      log "已从 deploy/.env.example 生成 .env"
    else
      [ "$DRY_RUN" -eq 1 ] || : > "$ENV_FILE"
      warn "没有 .env.example，已创建空 .env"
    fi
  else
    if [ "$DRY_RUN" -eq 0 ]; then
      cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
      info "已备份原 .env"
    fi
  fi

  local secret chrome
  secret="$(env_value SK_SECRET || true)"
  case "$secret" in
    ""|change-me*|CHANGE_ME*|your-secret*|please-change*)
      secret="$("$PY" -c 'import secrets;print(secrets.token_hex(32))')"
      if env_has SK_SECRET; then
        [ "$DRY_RUN" -eq 1 ] || awk -v v="$secret" \
          'BEGIN{done=0} { if (!done && $0 ~ "^[[:space:]]*SK_SECRET=" ) { print "SK_SECRET="v; done=1 } else print }' \
          "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
        log "已生成随机 SK_SECRET（替换掉示例值）"
      else
        [ "$DRY_RUN" -eq 1 ] || printf 'SK_SECRET=%s\n' "$secret" >> "$ENV_FILE"
        log "已生成随机 SK_SECRET"
      fi
      ;;
    *) info "SK_SECRET 已有自定义值，保持不变" ;;
  esac

  env_ensure SK_ENV prod
  # ⚠️ .env.example 的 SK_DATA_DIR 是 Docker 里的 /app/data；裸机上必须改成本机目录，
  #    否则 SQLite 会去写 /app/data（不存在或权限不对）→ 服务起来但接口全 500。
  local cur_data
  cur_data="$(env_value SK_DATA_DIR || true)"
  case "$cur_data" in
    ""|/app/data|/data)
      env_force SK_DATA_DIR "$DATA_DIR" ;;
    *)
      [ "$cur_data" = "$DATA_DIR" ] || warn "SK_DATA_DIR=$cur_data（与项目目录不同，确认是有意为之）" ;;
  esac
  env_ensure TZ Asia/Shanghai
  env_ensure SK_SCHEDULER_ENABLED 1
  env_ensure SK_UNLIMITED_TASK_RUNS 1
  chrome="$(find_chromium || true)"
  if [ -n "$chrome" ]; then
    # 只在「没配 / 配的路径不存在 / 还是模板默认值」时改写：
    # 用户自己填的、确实存在的自定义路径要保留（尊重手工配置）
    local cur_chrome
    cur_chrome="$(env_value SK_CHROME_PATH || true)"
    if [ -z "$cur_chrome" ] || [ ! -x "$cur_chrome" ] || [ "$cur_chrome" = "/usr/bin/chromium" ]; then
      env_force SK_CHROME_PATH "$chrome"
    else
      info "SK_CHROME_PATH 已是自定义路径：$cur_chrome（保留）"
    fi
  fi
  # 内存自适应：<3GB 只跑 1 个浏览器
  local mem_mb=0 default_conc=2
  mem_mb="$(awk '/^MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
  if [ "${mem_mb:-0}" -gt 0 ] && [ "$mem_mb" -lt 3000 ]; then
    default_conc=1
    warn "内存仅 ${mem_mb}MB：并发浏览器设为 1（宝塔面板自己也要吃内存）"
  fi
  env_ensure SK_MAX_CONCURRENT_BROWSERS "$default_conc"
  log ".env 已就绪：$ENV_FILE"
}

# ---------------------------------------------------------------------------
# 5) systemd 单元
# ---------------------------------------------------------------------------
install_unit() {
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "没有 systemctl（非 systemd 系统）：跳过安装，手动启动命令见最后"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] 生成 $UNIT_PATH（路径=$PROJECT_ROOT 用户=$SVC_USER 端口=$PORT）"
    return 0
  fi
  sed -e "s|/www/wwwroot/spark-keeper|$PROJECT_ROOT|g" \
      -e "s|^User=.*|User=$SVC_USER|" \
      -e "s|^Group=.*|Group=$SVC_GROUP|" \
      -e "s|Environment=SK_PORT=.*|Environment=SK_PORT=$PORT|" \
      -e "s|--port 8000|--port $PORT|" \
      -e "s|/usr/bin/python3|$PY|g" \
      "$UNIT_TEMPLATE" > "$UNIT_PATH"
  # HOME 必须可写：Chromium 在 HOME 里放临时配置，www 用户的 HOME 常常不可写
  if ! grep -qE '^Environment=HOME=' "$UNIT_PATH"; then
    sed -i "/^\[Service\]/a Environment=HOME=$DATA_DIR" "$UNIT_PATH"
  fi
  log "已写入 systemd 单元：$UNIT_PATH"
  run systemctl daemon-reload
}

# ---------------------------------------------------------------------------
# 6) 初始化 + 启动
# ---------------------------------------------------------------------------
init_db() {
  info "初始化数据库（幂等，已初始化过会直接跳过）"
  if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] 以 $SVC_USER 身份执行 main.py init"
    return 0
  fi
  if [ "$(id -u)" -eq 0 ] && [ "$SVC_USER" != "root" ]; then
    su -s /bin/sh "$SVC_USER" -c \
      "cd '$PROJECT_ROOT' && SK_ENV=prod SK_DATA_DIR='$DATA_DIR' TZ=Asia/Shanghai '$PY' main.py init" \
      || die "main.py init 失败（看上面的报错）"
  else
    ( cd "$PROJECT_ROOT" && SK_ENV=prod SK_DATA_DIR="$DATA_DIR" "$PY" main.py init ) || die "main.py init 失败"
  fi
}

start_service() {
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "没有 systemctl：请手动后台启动："
    printf '\n  cd %s && SK_ENV=prod SK_DATA_DIR=%s nohup %s main.py serve --host 127.0.0.1 --port %s > data/serve.log 2>&1 &\n\n' \
      "$PROJECT_ROOT" "$DATA_DIR" "$PY" "$PORT"
    return 0
  fi
  run systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  run systemctl restart "$SERVICE_NAME"
  info "已启动服务，等待健康检查通过…"
}

health_check() {
  [ "$DRY_RUN" -eq 1 ] && return 0
  local i body
  for i in $(seq 1 30); do
    if command -v curl >/dev/null 2>&1; then
      body="$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/v1/health" 2>/dev/null || true)"
    else
      body="$("$PY" - "http://127.0.0.1:$PORT/api/v1/health" <<'PY' 2>/dev/null || true
import sys, urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=3).read().decode()[:200])
except Exception:
    pass
PY
)"
    fi
    if printf '%s' "$body" | grep -q '"ok"'; then
      log "健康检查通过：http://127.0.0.1:$PORT/api/v1/health"
      return 0
    fi
    sleep 2
  done
  err "健康检查没通过（60 秒内没起来）"
  printf '\n排查：\n  systemctl status %s --no-pager -l\n  journalctl -u %s -n 80 --no-pager\n\n' \
    "$SERVICE_NAME" "$SERVICE_NAME"
  return 1
}

# ---------------------------------------------------------------------------
# 7) 收尾提示
# ---------------------------------------------------------------------------
print_next_steps() {
  local admin="$DATA_DIR/INITIAL_ADMIN.txt"
  printf '\n%s=========================================================%s\n' "$C_OK" "$C_OFF"
  printf '%s 部署完成%s\n' "$C_OK" "$C_OFF"
  printf '%s=========================================================%s\n\n' "$C_OK" "$C_OFF"

  if [ -f "$admin" ]; then
    printf '%s首次管理员账号（登录后请改密码并删除该文件）：%s\n' "$C_WARN" "$C_OFF"
    sed 's/^/    /' "$admin"
    printf '\n'
  else
    info "管理员账号已存在（data/INITIAL_ADMIN.txt 不在了，说明之前初始化过）"
  fi

  cat <<EOF
服务管理：
  systemctl status  $SERVICE_NAME --no-pager -l
  systemctl restart $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f          # 实时日志

宝塔里再加一步（必须），否则外网打不开：
  1) 网站 → 添加站点（域名；类型选「纯静态」即可，根目录随便）
  2) 站点设置 → 反向代理 → 目标 URL 填： http://127.0.0.1:$PORT
  3) 站点设置 → 配置文件，把下面这段加进去（默认 60s 超时会掐断扫码/发送）：
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
        proxy_http_version 1.1;
        client_max_body_size 10m;
  4) 站点设置 → SSL → Let's Encrypt 一键申请 → 打开「强制 HTTPS」

服务器安全组也要放行 80/443（宝塔面板端口另算）。

备份（宝塔「计划任务」→ Shell 脚本，每天 04:00）：
  cd $PROJECT_ROOT && $PY tools/backup.py --out /www/backup/spark --keep 14

自检：
  cd $PROJECT_ROOT && $PY tools/selftest.py
EOF
}

print_manual_start() {
  cat <<EOF

手动启动（没有 systemd 时）：
  cd $PROJECT_ROOT
  SK_ENV=prod SK_DATA_DIR=$DATA_DIR nohup $PY main.py serve --host 127.0.0.1 --port $PORT > data/serve.log 2>&1 &
  tail -f data/serve.log
EOF
}

# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------
uninstall() {
  need_root
  if command -v systemctl >/dev/null 2>&1; then
    info "停止并禁用 $SERVICE_NAME"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  fi
  if [ -f "$UNIT_PATH" ]; then
    rm -f "$UNIT_PATH"
    command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload || true
    log "已删除 $UNIT_PATH"
  else
    info "没有已安装的 unit：$UNIT_PATH"
  fi
  log "data/ 已保留（含数据库与登录态）：$DATA_DIR"
  info "想彻底清空：rm -rf '$DATA_DIR'（不可恢复，先备份！）"
  exit 0
}

# ---------------------------------------------------------------------------
# 体检模式
# ---------------------------------------------------------------------------
check_only() {
  need_root
  detect_os
  resolve_python
  check_layout
  resolve_user
  check_chromium
  check_sqlite
  SSL_OK=1; check_ssl || SSL_OK=0
  # 注意 `|| true`：check_port 发现冲突会 return 1，脚本开了 set -e，
  # 不加这一句会「体检到一半直接退出」，用户就看不到下面的汇总了。
  check_port || true
  if [ -f "$UNIT_PATH" ]; then
    log "systemd 单元已安装：$UNIT_PATH"
    if command -v systemctl >/dev/null 2>&1; then
      info "服务状态：$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)"
      if service_active; then
        # 服务已经在跑：直接探一次健康检查，比只看端口有用得多
        local body
        body="$(curl -fsS --max-time 4 "http://127.0.0.1:$PORT/api/v1/health" 2>/dev/null || true)"
        if printf '%s' "$body" | grep -q '"ok"'; then
          log "健康检查通过：http://127.0.0.1:$PORT/api/v1/health"
        else
          warn "服务是 active，但 /api/v1/health 没响应（可能刚重启，或端口与 unit 不一致）"
          info "  查日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
        fi
      fi
    fi
  else
    info "systemd 单元未安装（还没跑过安装）"
  fi
  if [ -d "$DATA_DIR" ]; then
    log "数据目录存在：$DATA_DIR"
  else
    info "数据目录还不存在（安装时会创建）"
  fi
  if [ -f "$ENV_FILE" ]; then
    log ".env 存在"
  else
    info ".env 不存在（安装时会从模板生成）"
  fi
  printf '\n'
  if [ "$ERRORS" -gt 0 ]; then
    printf '%s体检发现 %s 个问题，先处理再安装。%s\n' "$C_ERR" "$ERRORS" "$C_OFF"
    exit 1
  fi
  printf '%s体检通过，可以执行安装。%s\n' "$C_OK" "$C_OFF"
  exit 0
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
main() {
  printf '\n%s== spark-keeper 一键部署（宝塔 / 通用 Linux）==%s\n\n' "$C_OK" "$C_OFF"
  info "项目目录：$PROJECT_ROOT"

  [ "$UNINSTALL" -eq 1 ] && uninstall
  [ "$CHECK_ONLY" -eq 1 ] && check_only

  # --dry-run 不落盘，所以不要求 root：这样在任意机器上都能先看一眼计划
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$(id -u)" -ne 0 ]; then
      warn "当前不是 root：dry-run 只打印计划（真正安装需要 root）"
    fi
  else
    need_root
  fi
  detect_os
  resolve_python
  check_layout
  resolve_user
  # 端口被本项目服务自己占着是常态（重装/升级时），只有被别的进程占用才中断
  check_port || die "端口 $PORT 被其它进程占用，请按上面的提示处理后重试（或加 --port 换端口）"

  if [ "" -ne 1 ]; then
    die "Python 缺少 _ssl（OpenSSL）：登录/鉴权接口不会注册，装了也用不了。请按上面 3 种修法之一处理后重跑。"
  fi

  install_packages
  check_chromium
  check_sqlite
  SSL_OK=1; check_ssl || SSL_OK=0
  prepare_dirs
  write_env
  install_unit
  init_db
  start_service
  health_check || true

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\n%s[dry-run] 以上为计划，未做任何修改。去掉 --dry-run 即真正执行。%s\n\n' "$C_WARN" "$C_OFF"
    exit 0
  fi

  print_next_steps
  command -v systemctl >/dev/null 2>&1 || print_manual_start

  if [ "$ERRORS" -gt 0 ]; then
    printf '\n%s注意：过程中有 %s 处告警/失败，请按上面提示处理后再跑一次 --check。%s\n\n' \
      "$C_WARN" "$ERRORS" "$C_OFF"
    exit 1
  fi
}

main "$@"
