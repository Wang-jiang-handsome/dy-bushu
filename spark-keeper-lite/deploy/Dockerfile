# =============================================================================
#  spark-keeper 生产镜像（pro / lite 通用）
#
#  ⚠️ 注意：这里**故意不写** `# syntax=docker/dockerfile:1`。
#     那行会让 BuildKit 先去 docker.io 拉一个「Dockerfile 前端镜像」，而国内网络
#     经常连不上 registry-1.docker.io，于是构建第一步就 30 秒超时：
#         ERROR [app] resolve image config for docker-image://docker.io/docker/dockerfile:1
#     本文件没有用到任何 BuildKit 专属语法（heredoc / --mount=...），所以去掉那行
#     对构建结果没有任何影响，还少一次外部依赖。
#
#  构建上下文必须是**交付目录根**，也就是包含 main.py、app/、web/、deploy/ 的那一层：
#      cd spark-keeper-pro
#      docker build -f deploy/Dockerfile -t spark-keeper-pro:1.0.0 .
#  或者直接用 compose（已写好 context: .）：
#      docker compose up -d --build
#
#  国内网络建议同时做两件事（deploy/setup-docker-mirror.sh 会帮你配好第 1 件）：
#      1) 给 Docker 配镜像加速器（拉 python:3.11-slim 用）
#      2) 构建时指定 Debian 源镜像：
#         docker compose build --build-arg APT_MIRROR=mirrors.aliyun.com
#
#  SK_CHROME_LOW_RESOURCE=1 / SK_CDP_PAGE_TIMEOUT=45：
#  容器通常只有 1~2 核，抖音私信页很重。真实事故：弱 CPU 上页面只渲染出空壳
#  （正文只有「开启读屏标签 / 抖音聊天」），随后 Runtime.evaluate 直接超时 10s。
#  低配模式会关掉软件光栅/GPU 合成、限制光栅线程与渲染进程数，并把页面操作超时放宽。
#
#  SK_CHROME_NO_SANDBOX=1：容器里 Chromium 的 setuid sandbox 用不了
#  （非 root 没有 SUID helper，老宿主机内核也没开 user namespace），
#  不关会直接 No usable sandbox! 退出。程序内部也有 auto 判断，这里是双保险。
#
#  为什么不用 playwright：整套浏览器驱动是自己写的 CDP 客户端（标准库 socket），
#  镜像里只要有一个 chromium 可执行文件就够了，少一层依赖少一个版本冲突。
# =============================================================================
FROM python:3.11-slim

# Debian 源镜像（可选）：国内构建时设 APT_MIRROR=mirrors.aliyun.com 之类，
# 否则 apt-get update 也常常慢到超时。留空 = 用官方源。
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi

# 依赖只装这 5 个，每个都有明确理由：
#   chromium        自动化引擎通过 CDP 驱动它（--headless=new）
#   fonts-noto-cjk  没有中文字体，抖音页面截图里全是方块，线上排障等于瞎
#   tzdata          zoneinfo 需要它解析 Asia/Shanghai，否则退回固定 +08:00
#   ca-certificates SMTP over TLS、支付回调 HTTPS 需要根证书
#   curl            可选排障工具；HEALTHCHECK 用 python，不依赖它
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk \
        tzdata \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f >/dev/null 2>&1 || true

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    SK_ENV=prod \
    SK_HOST=0.0.0.0 \
    SK_PORT=8000 \
    SK_DATA_DIR=/app/data \
    SK_CHROME_PATH=/usr/bin/chromium \
    SK_CHROME_NO_SANDBOX=1 \
    SK_CHROME_LOW_RESOURCE=1 \
    SK_CDP_PAGE_TIMEOUT=45

WORKDIR /app

# 非 root 运行：容器里跑的是浏览器自动化 + 用户登录态文件，被拿下代价太高
RUN groupadd --gid 10001 spark \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin spark

COPY --chown=spark:spark . /app

RUN mkdir -p /app/data/sessions /app/data/screenshots /app/data/uploads /app/data/backups \
 && chown -R spark:spark /app/data \
 && chmod +x /app/deploy/entrypoint.sh

USER spark

VOLUME ["/app/data"]
EXPOSE 8000

# 探针打 /api/v1/health（免登录、免限流、会顺便自检数据库）
HEALTHCHECK --interval=30s --timeout=8s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ.get('SK_PORT','8000'), timeout=5).read()" || exit 1

# entrypoint 负责：等数据目录就绪 → 迁移 → 无管理员则初始化 → exec 传进来的命令
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["python", "main.py", "serve"]
