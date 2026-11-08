#!/usr/bin/env bash
# =============================================================================
#  spark-keeper · Docker 国内镜像加速配置（幂等，可重复执行）
#
#  解决这个报错：
#      ERROR [app] resolve image config for docker-image://docker.io/docker/dockerfile:1
#      failed to do request ... dial tcp ... i/o timeout
#  以及拉不动 python:3.11-slim 的问题。
#
#  它做什么：
#    1. 备份现有 /etc/docker/daemon.json
#    2. 合并写入 3~4 个国内镜像加速器（保留文件里其它配置项，比如 data-root）
#    3. 重启 docker，并实测拉一个很小的镜像验证是否真的通
#
#  用法：
#      bash deploy/setup-docker-mirror.sh                 # 用内置默认列表
#      bash deploy/setup-docker-mirror.sh --check         # 只体检，不改配置
#      bash deploy/setup-docker-mirror.sh --mirrors "a,b"  # 自定义（逗号分隔）
#      bash deploy/setup-docker-mirror.sh --dry-run       # 只打印要写什么
#      bash deploy/setup-docker-mirror.sh --restore       # 还原上一次的备份
# =============================================================================
set -Eeuo pipefail

DAEMON_JSON="/etc/docker/daemon.json"
BACKUP=""
MODE="apply"
DRY_RUN=0
CUSTOM_MIRRORS=""

# 2026 年国内仍可用的几个公开加速器（按经验顺序；失效了换下一个）
DEFAULT_MIRRORS=(
  "https://docker.m.daocloud.io"
  "https://docker.1ms.run"
  "https://docker.xuanyuan.me"
  "https://mirror.ccs.tencentyun.com"
)

if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_OK="$(tput setaf 2)"; C_WARN="$(tput setaf 3)"; C_ERR="$(tput setaf 1)"
  C_DIM="$(tput setaf 8)"; C_OFF="$(tput sgr0)"
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi
log()  { printf '%s[✓]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
info() { printf '%s[·]%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '%s[✗]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check)   MODE="check"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --restore) MODE="restore"; shift ;;
    --mirrors) CUSTOM_MIRRORS="${2:?--mirrors 需要参数，逗号分隔}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

need_root() { [ "$(id -u)" -eq 0 ] || die "需要 root 执行：sudo bash $0 $*"; }
has_docker() { command -v docker >/dev/null 2>&1; }

show_current() {
  if [ -f "$DAEMON_JSON" ]; then
    info "当前 $DAEMON_JSON："
    sed 's/^/    /' "$DAEMON_JSON"
  else
    info "$DAEMON_JSON 还不存在"
  fi
}

#: 从已有 daemon.json 里读出 registry-mirrors 之外的内容（用 python 合并最稳）
merge_config() {
  local mirrors_json="$1"
  "${PYTHON:-python3}" - "$DAEMON_JSON" "$mirrors_json" <<'PY'
import json, os, sys

path, mirrors_json = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path) and os.path.getsize(path) > 0:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:                     # noqa: BLE001
        print(f"WARN 现有配置文件不是合法 JSON，将重写：{exc}", file=sys.stderr)
        data = {}
if not isinstance(data, dict):
    data = {}
data["registry-mirrors"] = json.loads(mirrors_json)
# 顺手给一个更稳的默认日志策略（宝塔机器上磁盘容易被日志吃满）
data.setdefault("log-driver", "json-file")
data.setdefault("log-opts", {"max-size": "20m", "max-file": "5"})
print(json.dumps(data, ensure_ascii=False, indent=2))
PY
}

probe_pull() {
  local image="hello-world"
  info "实测拉取 $image …"
  if timeout 90 docker pull "$image" >/dev/null 2>&1; then
    log "镜像加速可用（$image 拉取成功）"
    return 0
  fi
  warn "仍然拉不动 —— 依次尝试："
  warn "  1) 换一组镜像：bash deploy/setup-docker-mirror.sh --mirrors \"https://其它加速器\""
  warn "  2) 宝塔面板：Docker → 设置 → 镜像加速，填同样的地址"
  warn "  3) 离线方案：在能上网的机器上 docker save/load（见 deploy/DEPLOY_PLATFORMS.md）"
  return 1
}

case "$MODE" in
  check)
    need_root
    has_docker || die "没装 docker"
    docker --version || true
    show_current
    info "docker 服务状态：$(systemctl is-active docker 2>/dev/null || echo unknown)"
    if [ -f "$DAEMON_JSON" ] && grep -q "registry-mirrors" "$DAEMON_JSON"; then
      log "已配置 registry-mirrors"
    else
      warn "还没配置镜像加速（拉镜像会走官方源，国内经常超时）"
    fi
    exit 0
    ;;

  restore)
    need_root
    latest="$(ls -1t "${DAEMON_JSON}".bak.* 2>/dev/null | head -n1 || true)"
    [ -n "$latest" ] || die "没有可还原的备份（${DAEMON_JSON}.bak.*）"
    cp "$latest" "$DAEMON_JSON"
    log "已还原：$latest"
    systemctl restart docker 2>/dev/null || true
    exit 0
    ;;

  apply)
    need_root
    has_docker || die "没装 docker（宝塔：Docker 菜单安装，或 curl -fsSL https://get.docker.com | sh）"

    if [ -n "$CUSTOM_MIRRORS" ]; then
      IFS=',' read -r -a MIRRORS <<< "$CUSTOM_MIRRORS"
    else
      MIRRORS=("${DEFAULT_MIRRORS[@]}")
    fi
    mirrors_json="$(printf '%s\n' "${MIRRORS[@]}" | "${PYTHON:-python3}" -c \
      'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
    info "将写入镜像加速器：$mirrors_json"

    show_current
    NEW_JSON="$(merge_config "$mirrors_json")"

    if [ "$DRY_RUN" -eq 1 ]; then
      printf '%s[dry-run] 将写入 %s：%s\n%s\n' "$C_DIM" "$DAEMON_JSON" "$C_OFF" "$NEW_JSON"
      exit 0
    fi

    mkdir -p "$(dirname "$DAEMON_JSON")"
    if [ -f "$DAEMON_JSON" ]; then
      BACKUP="${DAEMON_JSON}.bak.$(date +%Y%m%d-%H%M%S)"
      cp "$DAEMON_JSON" "$BACKUP"
      log "已备份原配置：$BACKUP"
    fi
    printf '%s\n' "$NEW_JSON" > "$DAEMON_JSON"
    log "已写入 $DAEMON_JSON"

    info "重启 docker …"
    systemctl restart docker 2>/dev/null || die "docker 重启失败：journalctl -u docker -n 50 --no-pager"
    sleep 2
    systemctl is-active --quiet docker && log "docker 已运行" || die "docker 没起来"

    probe_pull || true

    cat <<EOF

下一步（国内网络建议顺手把 Debian 源也换掉，否则 apt-get update 同样会卡）：
  cd /www/wwwroot/dy/spark-keeper-lite
  SK_APT_MIRROR=mirrors.aliyun.com docker compose build
  docker compose up -d

如果构建仍卡在拉取 python:3.11-slim，可先单独拉：
  docker pull python:3.11-slim
EOF
    ;;
esac
