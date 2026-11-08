"""Chrome DevTools Protocol 客户端（自研，零第三方依赖）。

为什么不用 playwright
---------------------
部署环境 pip 装不了包（见 docs/SHARED_CONTEXT.md），所以这里直接用
``subprocess`` 拉起 Chrome/Chromium，用自研的 WebSocket（``wsdump.py``）连它的
DevTools 端点，发 JSON-RPC 风格的 CDP 命令。

结构
----
* :func:`launch_chrome` —— 拉起浏览器进程，读 ``DevToolsActivePort`` 拿端口。
* :class:`CDPSession` —— 一条 CDP 连接（可带 ``sessionId``）：``send()`` 按 id
  关联响应、超时抛错、支持事件订阅。
* :func:`attach` / :func:`create_page` —— 连到已有页面 / 新开一个标签页。

可测性设计（本机沙箱禁止起子进程）
----------------------------------
``CDPSession`` 只依赖一个 **Transport** 协议（``send_text`` / ``recv_text`` /
``close``），真实实现是 :class:`~app.automation.wsdump.WebSocketClient`，
单测里换成假实现就能验证「id 关联 / 乱序响应 / 超时 / 错误上报 / 事件分发」
——这些恰恰是最容易写错又最难在真机上复现的部分。

已知不确定项（部署机首次运行需确认）
------------------------------------
* Chrome 111+ 对 DevTools 端点做 Origin 白名单校验：本实现**不发 Origin**
  并在启动参数里加 ``--remote-allow-origins=*`` 双保险（置信度：高，公开 issue 可查）。
* 带账号密码的 HTTP 代理需要 ``Fetch`` 域应答 ``Fetch.authRequired``
  （见 :func:`attach_proxy_auth`），本机无法验证真实代理，置信度：中。
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import chrome as chrome_mod
from . import humanize
from .wsdump import WebSocketClient, WebSocketClosed, WebSocketError, WebSocketTimeout

DEFAULT_TIMEOUT = 20.0
#: DevToolsActivePort 出现的等待时间。冷启动 + 首次建 profile 在机械盘上可能到 10s+。
LAUNCH_TIMEOUT = 30.0
#: 允许的 CDP 消息最大长度由 wsdump 控制（32MB）；这里只限制 HTTP JSON 响应。
HTTP_TIMEOUT = 6.0


class CDPError(Exception):
    """CDP 调用失败（协议错误、浏览器返回 error、连接断开）。"""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None,
                 method: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        self.method = method

    def __str__(self) -> str:                                  # pragma: no cover - 展示用
        base = super().__str__()
        return f"{self.method}: {base}" if self.method else base


class CDPTimeout(CDPError):
    """等待响应超时。"""


class ChromeLaunchError(CDPError):
    """浏览器起不来（找不到可执行文件、端口文件没出现、进程立刻退出）。"""


# --------------------------------------------------------------------------
# HTTP 小工具（urllib，零依赖）
# --------------------------------------------------------------------------
def http_json(url: str, *, timeout: float = HTTP_TIMEOUT, method: str = "GET",
              body: Any = None, headers: dict[str, str] | None = None) -> Any:
    """请求 DevTools 的 HTTP 端点并解析 JSON。

    DevTools 的 ``/json/*`` 是明文 HTTP（只监听 127.0.0.1），所以不需要 TLS。
    """
    data = None
    merged = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        merged["Content-Type"] = "application/json"
    merged.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:                                     # noqa: BLE001 - 读不到就算了
            pass
        raise CDPError(f"DevTools HTTP {exc.code}: {detail or exc.reason}", code=exc.code) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CDPError(f"连接 DevTools HTTP 失败: {exc}") from exc
    if not payload.strip():
        return None
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise CDPError(f"DevTools 返回了非 JSON 内容: {payload[:120]!r}") from exc


def parse_devtools_active_port(text: str) -> tuple[int, str]:
    """解析 ``DevToolsActivePort`` 文件内容。

    格式固定两行：第一行是端口，第二行是浏览器级 WebSocket 路径
    （``/devtools/browser/<uuid>``）。缺第二行时返回空串（只有端口也可用）。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        raise CDPError("DevToolsActivePort 是空文件")
    try:
        port = int(lines[0])
    except ValueError as exc:
        raise CDPError(f"DevToolsActivePort 第一行不是端口: {lines[0]!r}") from exc
    if not (0 < port < 65536):
        raise CDPError(f"DevToolsActivePort 端口越界: {port}")
    path = lines[1] if len(lines) > 1 else ""
    if path and not path.startswith("/"):
        path = "/" + path
    return port, path


def in_container() -> bool:
    """当前进程是否跑在容器里（Docker / Podman / LXC / K8s）。

    为什么需要：容器里 Chromium 的 setuid sandbox 通常不可用（非 root 用户没有
    SUID helper；老宿主机内核又没开 user namespace），启动时报：

        No usable sandbox! ... 请安装 chromium-sandbox ...
        [zygote_host_impl_linux.cc] FATAL

    真实事故：CentOS 7 宿主机上的 Docker 容器（非 root 用户跑）浏览器起不来。
    """
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        cgroup = ""
    if any(mark in cgroup for mark in ("docker", "kubepods", "lxc", "containerd", "podman")):
        return True
    # env 兜底：K8s / 某些托管环境会注入（也可能人为指定）
    return os.environ.get("SK_IN_CONTAINER", "") not in ("", "0")


def _is_windows() -> bool:
    """是否 Windows。抽成函数是为了单测能打桩（直接改 os.name 会让 pathlib 炸）。"""
    return os.name == "nt"


def needs_no_sandbox(settings: Any) -> bool:
    """是否要给 Chromium 加 ``--no-sandbox``。

    优先级：显式配置（``SK_CHROME_NO_SANDBOX=1/0``）> 自动判断（root 或容器内）。
    """
    mode = str(getattr(settings, "chrome_no_sandbox", "auto") or "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    # auto
    if _is_windows():
        return False
    try:
        if os.geteuid() == 0:                     # type: ignore[attr-defined]
            return True
    except AttributeError:                        # pragma: no cover - 非 POSIX
        return False
    return in_container()


def display_available() -> bool:
    """当前环境能不能显示 GUI 窗口（Linux 下看 DISPLAY / Wayland / X socket）。

    真实事故一：云主机上点「在窗口里登录」→ Chromium 退出：
        Missing X server or $DISPLAY / The platform failed to initialize.
    真实事故二：容器里（即使被要求开窗口）报 `Error: Can't open display:`。
    所以：开窗口前先问「有显示器吗」，没有就强制无头（二维码走网页扫码）。
    """
    if _is_windows() or sys.platform == "darwin":
        return True
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    try:
        x11 = Path("/tmp/.X11-unix")
        if x11.is_dir() and any(x11.iterdir()):
            return True
    except OSError:
        pass
    return False


def effective_headless(settings: Any) -> bool:
    """实际要不要用无头模式：配置说无头 **或** 环境没有图形界面。"""
    if bool(getattr(settings, "browser_headless", True)):
        return True
    return not display_available()


def _clear_stale_profile_lock(profile: Path) -> list[str]:
    """清掉 Chromium 的 profile 单例锁（换机器 / 换容器后必然残留）。

    Chromium 在 profile 里放 ``SingletonLock``（内容含 hostname-pid）等文件；
    如果 profile 目录是**从别的机器复制来的**（或容器 hostname 变了），
    它会认为「另一个 Chromium 进程在别的电脑上占着这个 profile」并拒绝启动：

        The profile appears to be in use by another Chromium process (1592)
        on another computer (f8d034fc1c4d). Chromium has locked the profile ...

    真实事故：用户把 Windows 的 data/ 拷到 Linux 服务器（或容器重建后 hostname 变），
    任务和扫码登录都起不来。这里在启动前判断：锁记录的 hostname 与当前不同、
    或记录的 PID 已经不存在 → 直接删掉锁文件。
    """
    removed: list[str] = []
    # 先判断 SingletonLock 是否「活的」：三个锁文件是一组，要么全留、要么全清，
    # 只清 Socket/Cookie 而留下 Lock 会让 Chromium 处于半残状态。
    lock = profile / "SingletonLock"
    lock_live = False
    if lock.exists() or lock.is_symlink():
        try:
            raw = os.readlink(lock) if lock.is_symlink() else lock.read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        host, _, pid_text = str(raw).rpartition("-")
        if host and host != _hostname():
            lock_live = False           # 别的机器留下的锁 → 清
        elif pid_text.isdigit():
            lock_live = _pid_alive(int(pid_text))
        else:
            lock_live = False

    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = profile / name
        if not path.exists() and not path.is_symlink():
            continue
        if lock_live:
            continue
        try:
            path.unlink()
            removed.append(name)
        except OSError:
            try:
                path.rmdir()            # Socket 可能是目录
                removed.append(name)
            except OSError:
                pass
    if removed:
        try:
            from ..common import logging as _sklog

            _sklog.info(f"已清理残留的浏览器 profile 锁：{', '.join(removed)}",
                        scope="worker", source="cdp.profile_lock")
        except Exception:               # noqa: BLE001 - 日志失败不能影响启动
            pass
    return removed


def _hostname() -> str:
    try:
        return str(socket.gethostname() or "")
    except OSError:
        return ""


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。

    ⚠️ Windows 上**不能**用 `os.kill(pid, 0)` 探测：Windows 的 os.kill 走
    TerminateProcess，sig=0 也可能真的把进程杀掉（实测直接把测试进程干掉了）。
    所以 Windows 上保守认为「活着」，只在 hostname 不同时才判定锁失效。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def build_chrome_args(settings: Any, profile_dir: Path | str, *, proxy: Any = None,
                      port: int = 0, extra_args: Iterable[str] = (),
                      start_url: str = "about:blank") -> list[str]:
    """拼 Chrome 命令行参数（纯函数，单测直接断言）。

    关键参数及其原因::

        --headless=new              无头模式；服务端部署没有显示器
        --remote-debugging-port=0   让内核分配空闲端口，避免多实例端口冲突
        --user-data-dir=<profile>   每个抖音账号一个独立 profile（登录态隔离）
        --no-first-run / --no-default-browser-check  跳过首次运行向导（否则会弹窗阻塞）
        --disable-blink-features=AutomationControlled  去掉最明显的自动化特征
        --remote-allow-origins=*    Chrome 111+ 的 WS Origin 白名单（见模块 docstring）
        --disable-dev-shm-usage     容器里 /dev/shm 很小，不关会随机崩
    """
    width = int(getattr(settings, "browser_width", 1366) or 1366)
    height = int(getattr(settings, "browser_height", 900) or 900)
    profile = str(Path(profile_dir).resolve())
    args: list[str] = []
    if effective_headless(settings):
        args.append("--headless=new")
    args += [
        f"--remote-debugging-port={int(port)}",
        f"--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        f"--window-size={width},{height}",
        "--window-position=0,0",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        # CalculateNativeWinOcclusion：Chromium 认为窗口被遮挡时会**暂停渲染**，
        # 无头/最小化窗口下这条会命中，表现为「页面壳出来了但列表永远不渲染」。
        "--disable-features=Translate,OptimizationHints,MediaRouter,"
        "CalculateNativeWinOcclusion",
        "--hide-scrollbars",
        "--mute-audio",
        "--no-service-autorun",
        "--password-store=basic",
        "--use-mock-keychain",
        "--lang=zh-CN",
        "--accept-lang=zh-CN,zh,en",
        # 弱 CPU（1~2 核容器）上的稳定项：软件光栅/GPU 合成非常吃 CPU，
        # 抖音私信页又重，不开这些很容易「渲染进程假死、JS 全部超时」。
        "--disable-software-rasterizer",
        "--disable-gpu-compositing",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-sync",
        "--no-pings",
    ]
    if bool(getattr(settings, "chrome_low_resource", False)):
        args += [
            "--num-raster-threads=1",
            "--renderer-process-limit=2",
            "--js-flags=--max-old-space-size=512",
        ]
    if not _is_windows() and needs_no_sandbox(settings):
        # 容器 / root：setuid sandbox 不可用，必须关掉，否则浏览器直接退出：
        #   No usable sandbox! ... chromium-sandbox ...
        # 加 --disable-setuid-sandbox 是双保险（老 Chromium 单独看这个开关）。
        args.append("--no-sandbox")
        args.append("--disable-setuid-sandbox")
    proxy_arg = proxy_server_arg(proxy)
    if proxy_arg:
        args.append(proxy_arg)
    args.extend(str(item) for item in extra_args)
    args.append(start_url)
    return args


def _diagnose_chrome_failure(tail: str, settings: Any = None) -> str:
    """根据浏览器日志尾部给出**可执行的**排查建议（返回空串表示没有命中已知模式）。

    这些都是真实踩过的坑，直接把结论写进错误消息，省得用户对着 chromium 原文猜。
    """
    text = str(tail or "").lower()
    hints: list[str] = []
    if "no usable sandbox" in text or "zygote_host_impl_linux" in text or "suid sandbox" in text:
        hints.append(
            "命中「沙箱不可用」：容器 / 老内核宿主机上 setuid sandbox 用不了。\n"
            "      修法：设置环境变量 SK_CHROME_NO_SANDBOX=1 后重启服务"
            "（程序会自动加 --no-sandbox；Docker 部署时 compose 已默认开启）。"
        )
    if "failed to create shared memory" in text or "dev/shm" in text or "shared memory region" in text:
        hints.append(
            "命中「/dev/shm 太小」：Docker 里给容器加 --shm-size=1g（compose 已配 shm_size: 1gb），"
            "或保留 --disable-dev-shm-usage。"
        )
    if "libnss3" in text or "cannot open shared object file" in text:
        hints.append(
            "命中「缺系统库」：缺 Chromium 运行库（libnss3 / libatk / libgbm 等）。\n"
            "      Ubuntu/Debian 上装：apt-get install -y chromium（本项目镜像已装）；"
            "裸机请用 deploy/baota-install.sh 自动装。"
        )
    if "profile appears to be in use" in text or "singletonlock" in text:
        hints.append(
            "命中「profile 被占用」：多半是 data/ 从别的机器（或旧容器）拷过来的，"
            "里面残留了 Chromium 单例锁。\n"
            "      程序已会自动清理 SingletonLock/Socket/Cookie；仍失败就手动删："
            "rm -f data/profiles/*/Singleton*"
        )
    if ("missing x server" in text or "could not connect to display" in text
            or "can't open display" in text or "cannot open display" in text):
        hints.append(
            "命中「没有图形界面」：服务器/容器里没有 X，不能用可见窗口模式。\n"
            "      程序已会自动降级成无头浏览器（二维码显示在网页里）。\n"
            "      想用窗口登录：本机运行服务，或用带桌面的机器。"
        )
    if "cannot find" in text and "binary" in text:
        hints.append("命中「找不到可执行文件」：检查 SK_CHROME_PATH 是否指向真实 chromium。")
    if not hints:
        return ""
    mode = str(getattr(settings, "chrome_no_sandbox", "auto") or "auto")
    return "\n    排查建议（SK_CHROME_NO_SANDBOX 当前=%s）：\n    - %s" % (mode, "\n    - ".join(hints))


def proxy_server_arg(proxy: Any) -> str:
    """把代理配置转成 ``--proxy-server=`` 参数。

    接受 ``http://user:pass@host:port`` 字符串，或 ``proxies`` 表的 dict
    （``scheme/host/port/username/password``）。**用户名密码不放进命令行**
    （会出现在 ``ps`` 里、也会被 Chrome 写进日志），改用
    :func:`attach_proxy_auth` 通过 CDP 应答认证。
    """
    if not proxy:
        return ""
    if isinstance(proxy, str):
        return f"--proxy-server={proxy}"
    scheme = str(proxy.get("scheme") or "http").lower()
    host = str(proxy.get("host") or "").strip()
    if not host:
        return ""
    port = int(proxy.get("port") or 0)
    if port <= 0:
        return ""
    if scheme not in ("http", "https", "socks5"):
        scheme = "http"
    return f"--proxy-server={scheme}://{host}:{port}"


# --------------------------------------------------------------------------
# 浏览器进程
# --------------------------------------------------------------------------
@dataclass
class ChromeProcess:
    """一个受我们控制的 Chrome 进程。"""

    pid: int
    port: int
    profile_dir: Path
    browser_path: str = ""
    browser_ws_path: str = ""
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    started_at: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    # ---- 端点 ----
    def http_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def version(self, timeout: float = HTTP_TIMEOUT) -> dict:
        """``GET /json/version``（含 ``webSocketDebuggerUrl`` 与浏览器版本）。"""
        return http_json(f"{self.http_base()}/json/version", timeout=timeout) or {}

    def browser_ws_url(self, timeout: float = HTTP_TIMEOUT) -> str:
        """浏览器级 WebSocket 地址（``Target.createTarget`` 等浏览器命令走它）。"""
        info = self.version(timeout)
        url = str(info.get("webSocketDebuggerUrl") or "")
        if url:
            return url
        if self.browser_ws_path:
            return f"ws://127.0.0.1:{self.port}{self.browser_ws_path}"
        raise CDPError("DevTools 没有返回 webSocketDebuggerUrl（浏览器可能未就绪）")

    def targets(self, timeout: float = HTTP_TIMEOUT) -> list[dict]:
        """``GET /json/list``：所有可调试目标（page/service_worker/...）。"""
        items = http_json(f"{self.http_base()}/json/list", timeout=timeout)
        return [item for item in (items or []) if isinstance(item, dict)]

    def page_targets(self, timeout: float = HTTP_TIMEOUT) -> list[dict]:
        return [t for t in self.targets(timeout) if t.get("type") == "page"]

    # ---- 生命周期 ----
    def is_alive(self) -> bool:
        if self.process is None:
            return self._probe_port()
        return self.process.poll() is None

    def _probe_port(self, timeout: float = 1.0) -> bool:
        try:
            http_json(f"{self.http_base()}/json/version", timeout=timeout)
            return True
        except CDPError:
            return False

    def uptime(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def log_tail(self, limit: int = 1200) -> str:
        """尾部日志：起不来时用来解释原因（比「启动失败」四个字有用得多）。"""
        if not self.log_path or not self.log_path.exists():
            return ""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]

    def close(self, timeout: float = 5.0) -> None:
        """先 SIGTERM 再 kill，确保不留孤儿子进程（容器里最怕这个）。"""
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:         # pragma: no cover
                        pass
        except OSError:                                       # pragma: no cover - 进程已消失
            pass

    def kill_tree(self) -> bool:
        """强制结束进程树（Windows 上 Chrome 会 fork 出多个子进程）。"""
        if not self.pid:
            return False
        try:
            if os.name == "nt":
                # 不引第三方库，也不起 shell：直接对主进程强杀，Chrome 的子进程
                # 会因父进程退出而自行结束（实测可接受，残留由 pool.cleanup 兜底）。
                os.kill(self.pid, signal.SIGTERM)
            else:
                os.killpg(os.getpgid(self.pid), signal.SIGTERM)   # type: ignore[attr-defined]
            return True
        except (OSError, AttributeError, ProcessLookupError):
            return False

    def snapshot(self, *, profile_name: str = "") -> dict:
        """管理端 ``/admin/browser-processes`` 用的快照。

        **绝不放 storage_path / cookies**：这里只有进程元信息。
        """
        return {
            "pid": self.pid,
            "port": self.port,
            "profile": profile_name or self.profile_dir.name,
            "browser": Path(self.browser_path).name if self.browser_path else "",
            "alive": self.is_alive(),
            "uptime_seconds": int(self.uptime()),
            "log": self.log_path.name if self.log_path else "",
        }

    def __enter__(self) -> "ChromeProcess":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def launch_chrome(settings: Any, profile_dir: Path | str, proxy: Any = None, *,
                  extra_args: Iterable[str] = (), start_url: str = "about:blank",
                  timeout: float = LAUNCH_TIMEOUT) -> ChromeProcess:
    """拉起浏览器并等它就绪，返回 :class:`ChromeProcess`。

    等就绪的判据是 ``<profile>/DevToolsActivePort`` 文件出现（Chrome 在开始
    监听后才写它），比「睡 3 秒」可靠；期间如果进程已经退出，立刻报错并把
    日志尾部带出来。
    """
    profile = Path(profile_dir).expanduser()
    # 先探测浏览器再建目录：没有浏览器时不该留下一堆空 profile 目录
    browser = chrome_mod.find_browser(getattr(settings, "chrome_path", "") or "")
    if not browser:
        raise ChromeLaunchError(
            "未找到 Chrome/Chromium/Edge。请安装浏览器，或用 SK_CHROME_PATH 指定路径。"
        )
    profile.mkdir(parents=True, exist_ok=True)
    # 换机器/换容器后 profile 里的单例锁会让 Chromium 拒绝启动，先清掉
    _clear_stale_profile_lock(profile)
    args = build_chrome_args(settings, profile, proxy=proxy, extra_args=extra_args,
                             start_url=start_url)
    log_path = profile / "chrome.log"
    # stdout/stderr 重定向到文件而不是 PIPE：PIPE 不读会写满缓冲区把 Chrome 卡死
    # （DevTools 本身很安静，但渲染进程崩溃信息会往 stderr 写）。
    try:
        log_file = open(log_path, "ab")
    except OSError:
        log_file = open(os.devnull, "ab")
    try:
        popen_kwargs: dict[str, Any] = {
            "stdout": log_file,
            "stderr": log_file,
            "stdin": subprocess.DEVNULL,
            "cwd": str(profile),
            "close_fds": True,
        }
        if os.name != "nt":
            # 单独进程组，便于整组回收
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen([browser, *args], **popen_kwargs)
    except OSError as exc:
        log_file.close()
        raise ChromeLaunchError(f"启动浏览器失败: {exc}") from exc
    finally:
        try:
            log_file.close()          # 父进程不需要持有句柄，子进程已 dup
        except OSError:               # pragma: no cover
            pass

    deadline = time.monotonic() + max(1.0, timeout)
    active_port_file = profile / "DevToolsActivePort"
    chrome_process = ChromeProcess(
        pid=int(process.pid or 0), port=0, profile_dir=profile, browser_path=browser,
        process=process, log_path=log_path,
    )
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = chrome_process.log_tail(600)
                raise ChromeLaunchError(
                    f"浏览器启动后立即退出（exit={process.returncode}）。"
                    f"日志尾部：{tail}{_diagnose_chrome_failure(tail, settings)}"
                )
            if active_port_file.exists():
                try:
                    text = active_port_file.read_text(encoding="utf-8", errors="replace")
                    port, ws_path = parse_devtools_active_port(text)
                except (OSError, CDPError):
                    time.sleep(0.1)     # 文件刚出现可能还没写完
                    continue
                chrome_process.port = port
                chrome_process.browser_ws_path = ws_path
                # 再验一次 HTTP 端点真的活了（文件存在 ≠ HTTP 就绪）
                if chrome_process._probe_port(timeout=2.0):
                    return chrome_process
            time.sleep(0.15)
        raise ChromeLaunchError(
            f"等待 DevToolsActivePort 超时（{timeout:.0f}s）。"
            f"日志尾部：{chrome_process.log_tail(600)}"
        )
    except Exception:
        chrome_process.close(timeout=3.0)
        raise


# --------------------------------------------------------------------------
# CDP 会话
# --------------------------------------------------------------------------
class _Connection:
    """一条 WebSocket 连接上的多路复用：id 关联、事件分发、读线程。"""

    def __init__(self, transport: Any, *, recv_timeout: float = 1.0) -> None:
        self.transport = transport
        self.recv_timeout = max(0.1, float(recv_timeout))
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}
        self._events: dict[str, list[Callable[[dict], None]]] = {}
        self._lock = threading.RLock()
        self._closed = False
        self.error: Exception | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- 请求 ----
    def next_id(self) -> int:
        return next(self._ids)

    def request(self, message: dict, *, timeout: float) -> dict:
        message = {"id": message.pop("id"), **message}
        slot = {"event": threading.Event(), "result": None, "error": None}
        with self._lock:
            if self._closed:
                raise CDPError("CDP 连接已关闭", method=str(message.get("method") or ""))
            self._pending[int(message["id"])] = slot
        try:
            self.transport.send_text(json.dumps(message, ensure_ascii=False))
            if not slot["event"].wait(timeout):
                raise CDPTimeout(
                    f"等待 CDP 响应超时（{timeout:.0f}s）",
                    method=str(message.get("method") or ""),
                )
        finally:
            with self._lock:
                self._pending.pop(int(message["id"]), None)
        if slot["error"] is not None:
            raise slot["error"]
        return slot["result"] or {}

    # ---- 分发 ----
    def dispatch(self, raw: str | bytes) -> bool:
        """把一条原始 CDP 消息路由到等待者或事件订阅者。

        返回 ``True`` 表示这条消息被处理（有归属），``False`` 表示无法识别
        （例如别人 sessionId 的消息），调用方可以打日志。
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", errors="replace")
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not isinstance(message, dict):
            return False

        msg_id = message.get("id")
        if msg_id is not None:
            with self._lock:
                slot = self._pending.get(int(msg_id))
            if slot is None:
                return False                     # 已经超时被丢弃的响应，正常情况
            error = message.get("error")
            if error:
                slot["error"] = CDPError(
                    str(error.get("message") or "CDP 错误"),
                    code=int(error.get("code") or 0) or None,
                    data=error.get("data"),
                    method=str(message.get("method") or ""),
                )
            else:
                slot["result"] = message.get("result") or {}
            slot["event"].set()
            return True

        method = message.get("method")
        if not method:
            return False
        self._emit(str(method), message)
        return True

    def _emit(self, method: str, message: dict) -> None:
        with self._lock:
            handlers = list(self._events.get(method, ())) + list(self._events.get("*", ()))
        for handler in handlers:
            try:
                handler(message)
            except Exception:       # noqa: BLE001 - 订阅者自己的异常不能打断读循环
                pass

    def on(self, event: str, handler: Callable[[dict], None]) -> Callable[[dict], None]:
        with self._lock:
            self._events.setdefault(event, []).append(handler)
        return handler

    def off(self, event: str, handler: Callable[[dict], None]) -> None:
        with self._lock:
            bucket = self._events.get(event)
            if bucket and handler in bucket:
                bucket.remove(handler)

    # ---- 读线程 ----
    def start_reader(self) -> None:
        if self._reader and self._reader.is_alive():
            return
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:                              # pragma: no cover - 线程体
        while not self._stop.is_set():
            try:
                raw = self.transport.recv_text(self.recv_timeout)
            except WebSocketTimeout:
                continue
            except (WebSocketClosed, OSError) as exc:
                self._fail_all(CDPError(f"CDP 连接已断开: {exc}"))
                return
            except WebSocketError as exc:
                self._fail_all(CDPError(f"CDP WebSocket 错误: {exc}"))
                return
            except Exception as exc:            # noqa: BLE001 - 读线程必须自己兜底
                self._fail_all(CDPError(f"CDP 读线程异常: {type(exc).__name__}: {exc}"))
                return
            self.dispatch(raw)

    def _fail_all(self, error: Exception) -> None:
        self.error = error
        with self._lock:
            self._closed = True
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["error"] = error
            slot["event"].set()

    def close(self) -> None:
        self._stop.set()
        self._closed = True
        try:
            self.transport.close()
        except Exception:                       # noqa: BLE001 - 关连接失败无需处理
            pass
        error = CDPError("CDP 连接已关闭")
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["error"] = error
            slot["event"].set()


@dataclass(frozen=True)
class KeySpec:
    key: str
    code: str
    vk: int
    text: str = ""


#: 常用按键。CDP 的 ``Input.dispatchKeyEvent`` 需要 windowsVirtualKeyCode，
#: 少了它有些页面（React 合成事件）收不到回车。
KEYMAP: dict[str, KeySpec] = {
    "enter": KeySpec("Enter", "Enter", 13, "\r"),
    "tab": KeySpec("Tab", "Tab", 9),
    "escape": KeySpec("Escape", "Escape", 27),
    "backspace": KeySpec("Backspace", "Backspace", 8),
    "delete": KeySpec("Delete", "Delete", 46),
    "arrowleft": KeySpec("ArrowLeft", "ArrowLeft", 37),
    "arrowup": KeySpec("ArrowUp", "ArrowUp", 38),
    "arrowright": KeySpec("ArrowRight", "ArrowRight", 39),
    "arrowdown": KeySpec("ArrowDown", "ArrowDown", 40),
    "space": KeySpec(" ", "Space", 32, " "),
    "home": KeySpec("Home", "Home", 36),
    "end": KeySpec("End", "End", 35),
}


class CDPSession:
    """一条 CDP 会话。

    ``session_id`` 非空时表示它是某个 target 的扁平化（flatten）子会话，
    所有命令都会带上 ``sessionId``（Chrome 的多 target 复用单连接的机制）。
    """

    def __init__(self, transport: Any, *, default_timeout: float = DEFAULT_TIMEOUT,
                 session_id: str = "", target_id: str = "", connection: _Connection | None = None,
                 recv_timeout: float = 1.0, owns_transport: bool = True) -> None:
        self._conn = connection or _Connection(transport, recv_timeout=recv_timeout)
        self._owns = owns_transport and connection is None
        self.default_timeout = float(default_timeout)
        self.session_id = session_id
        self.target_id = target_id
        self.url = ""

    # ---- 构造 ----
    @classmethod
    def connect(cls, ws_url: str, *, timeout: float = 10.0,
                default_timeout: float = DEFAULT_TIMEOUT, recv_timeout: float = 1.0,
                session_id: str = "", target_id: str = "") -> "CDPSession":
        """连到 ``ws://127.0.0.1:<port>/devtools/...`` 并启动读线程。"""
        transport = WebSocketClient.connect(ws_url, timeout=timeout, recv_timeout=recv_timeout)
        session = cls(transport, default_timeout=default_timeout, session_id=session_id,
                      target_id=target_id, recv_timeout=recv_timeout)
        session.url = ws_url
        session._conn.start_reader()
        return session

    def child(self, session_id: str, *, target_id: str = "") -> "CDPSession":
        """同一连接上的子会话（``Target.attachToTarget {flatten:true}`` 之后用）。"""
        return CDPSession(self._conn.transport, default_timeout=self.default_timeout,
                          session_id=session_id, target_id=target_id,
                          connection=self._conn, owns_transport=False)

    # ---- 基础收发 ----
    @property
    def connected(self) -> bool:
        return not self._conn._closed                      # noqa: SLF001 - 同模块内部状态

    def send(self, method: str, params: dict | None = None, *, timeout: float | None = None,
             session_id: str | None = None) -> dict:
        """发一条 CDP 命令并等响应；失败抛 :class:`CDPError` / :class:`CDPTimeout`。"""
        message: dict[str, Any] = {"id": self._conn.next_id(), "method": method}
        if params:
            message["params"] = params
        target_session = self.session_id if session_id is None else session_id
        if target_session:
            message["sessionId"] = target_session
        return self._conn.request(message, timeout=float(timeout or self.default_timeout))

    def dispatch_message(self, raw: str | bytes) -> bool:
        """把一条原始消息喂给会话（单测用；生产由读线程调用）。"""
        return self._conn.dispatch(raw)

    def on(self, event: str, handler: Callable[[dict], None]) -> Callable[[dict], None]:
        return self._conn.on(event, handler)

    def off(self, event: str, handler: Callable[[dict], None]) -> None:
        self._conn.off(event, handler)

    def wait_event(self, event: str, *, timeout: float = 20.0,
                   predicate: Callable[[dict], bool] | None = None) -> dict:
        """等一个 CDP 事件（``predicate`` 为假则继续等）。"""
        result: dict = {}
        done = threading.Event()

        def handler(message: dict) -> None:
            if predicate and not predicate(message):
                return
            result.update(message)
            done.set()

        self.on(event, handler)
        try:
            if not done.wait(timeout):
                raise CDPTimeout(f"等待事件 {event} 超时（{timeout:.0f}s）", method=event)
        finally:
            self.off(event, handler)
        return result

    # ---- 域开关 ----
    def enable_foreground(self) -> None:
        """让页面相信自己在前台并被聚焦（无头模式下默认可能是 hidden）。

        真实事故：抖音私信页在无头浏览器里只渲染出空壳
        （正文只有「开启读屏标签 / 抖音聊天」，会话条数 0、无任何错误），
        随后 `Runtime.evaluate` 直接 8 秒无响应 —— 典型的「页面被判定为后台，
        渲染与定时器被节流」。这里用 CDP 强制前台：

        * ``Emulation.setFocusEmulationEnabled`` → document.hasFocus() 为 true
        * ``Page.setWebLifecycleState(active)`` → 页面不会被冻结
        * 再把 ``document.visibilityState`` 拉一次确认

        失败不影响主流程（老版本 Chromium 可能没有这些方法）。
        """
        for method, params in (
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ("Page.setWebLifecycleState", {"state": "active"}),
        ):
            try:
                self.send(method, params, timeout=5.0)
            except CDPError:
                pass

    def enable_domains(self, *, page: bool = True, runtime: bool = True,
                       network: bool = True, dom: bool = False) -> None:
        """按需开启 CDP 域。

        有些命令（``Network.getCookies``/``setCookies``）必须先 ``Network.enable``，
        否则会返回空列表 —— 这是最容易踩的坑，所以集中在这里做。
        """
        if page:
            self.send("Page.enable")
        if runtime:
            self.send("Runtime.enable")
        if network:
            self.send("Network.enable")
        if dom:
            self.send("DOM.enable")

    # ---- 执行 JS ----
    def evaluate(self, expression: str, *, await_promise: bool = False,
                 return_by_value: bool = True, timeout: float | None = None,
                 user_gesture: bool = True, context_id: int | None = None) -> Any:
        """执行 JS 表达式，返回 JSON 值。

        页面里抛异常时**必须报错**而不是返回 ``None``：否则「选择器写错」和
        「页面没有这个元素」会混在一起，排障时非常痛苦。
        """
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": await_promise,
            "userGesture": user_gesture,
        }
        if context_id:
            params["contextId"] = int(context_id)
        result = self.send("Runtime.evaluate", params, timeout=timeout)
        details = result.get("exceptionDetails")
        if details:
            text = ""
            exception = details.get("exception") or {}
            text = str(exception.get("description") or exception.get("value")
                       or details.get("text") or "JS 执行异常")
            raise CDPError(text[:500], method="Runtime.evaluate")
        remote = result.get("result") or {}
        if return_by_value:
            if remote.get("type") == "undefined":
                return None
            return remote.get("value")
        return remote

    def evaluate_async(self, expression: str, *, timeout: float | None = None) -> Any:
        """等待 Promise 结果（``await_promise=True``）。"""
        return self.evaluate(expression, await_promise=True, timeout=timeout)

    def safe_evaluate(self, expression: str, default: Any = None,
                      timeout: float | None = None) -> Any:
        """探测式求值：失败返回 ``default``。

        只在「拿不到也不影响主流程」的场景用（例如读一个可选状态、
        诊断脚本里逐条试选择器）；**判断发送是否成功这类关键路径不要用它**，
        否则会把错误静默掉。
        """
        try:
            return self.evaluate(expression, timeout=timeout)
        except CDPError:
            return default

    def call(self, fn_source: str, *args: Any, timeout: float | None = None) -> Any:
        """调用一段 JS 函数源码并传参。

        ``fn_source`` 形如 ``"(payload) => { ... }"``；参数用 JSON 展开传入，
        避免手工拼字符串带来的转义灾难。
        """
        payload = json.dumps([*args], ensure_ascii=False)
        expression = f"({fn_source})(...{payload})"
        return self.evaluate(expression, timeout=timeout)

    def wait_for(self, expression: str, *, timeout: float = 20.0, interval: float = 0.25) -> bool:
        """轮询直到表达式为真。返回 ``False`` 而不是抛错，由调用方决定怎么处理。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            try:
                if self.evaluate(expression):
                    return True
            except CDPError:
                # 页面正在导航时 Runtime.evaluate 会短暂失败（执行上下文销毁），
                # 这不是错误，继续等即可。
                pass
            time.sleep(interval)
        return False

    # ---- 页面 ----
    def navigate(self, url: str, *, timeout: float = 30.0, wait_ready: bool = True) -> dict:
        """导航到 URL。

        ``wait_ready`` 用轮询 ``document.readyState`` 而不是监听
        ``Page.loadEventFired``：SPA 页面加载事件常常在注册监听前就触发完了，
        轮询没有这个竞态。
        """
        result = self.send("Page.navigate", {"url": url}, timeout=timeout)
        if wait_ready:
            self.wait_for("document.readyState === 'complete'", timeout=min(timeout, 20.0),
                          interval=0.2)
        return result or {}

    def current_url(self) -> str:
        return str(self.safe_evaluate("location.href", "") or "")

    def title(self) -> str:
        return str(self.safe_evaluate("document.title", "") or "")

    def html(self) -> str:
        return str(self.safe_evaluate("document.documentElement.outerHTML", "") or "")

    def new_target(self, url: str = "about:blank") -> dict:
        """新开标签页（需要浏览器级会话）。返回 ``{targetId: ...}``。"""
        return self.send("Target.createTarget", {"url": url})

    def attach_to_target(self, target_id: str, *, flatten: bool = True) -> "CDPSession":
        """附加到某个 target。``flatten=True`` 时返回共享连接的子会话。"""
        result = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": flatten})
        session_id = str(result.get("sessionId") or "")
        if not session_id:
            raise CDPError(f"attachToTarget 未返回 sessionId: {result}")
        return self.child(session_id, target_id=target_id)

    # ---- 存储 / 身份 ----
    def get_cookies(self, urls: Iterable[str] | None = None) -> list[dict]:
        params = {"urls": list(urls)} if urls else {}
        result = self.send("Network.getCookies", params)
        return [c for c in (result.get("cookies") or []) if isinstance(c, dict)]

    def set_cookies(self, cookies: Iterable[dict]) -> int:
        """写入 cookies。返回成功条数。

        优先用 ``Network.setCookies``（批量，一次 RPC）；老版本 Chrome 没有它，
        退化成逐条 ``Network.setCookie``。
        """
        items = [clean_cookie(c) for c in cookies or []]
        items = [c for c in items if c.get("name") and c.get("domain")]
        if not items:
            return 0
        try:
            self.send("Network.setCookies", {"cookies": items})
            return len(items)
        except CDPError:
            ok = 0
            for cookie in items:
                try:
                    result = self.send("Network.setCookie", cookie)
                    if result.get("success", True):
                        ok += 1
                except CDPError:
                    continue
            return ok

    def clear_cookies(self) -> None:
        self.send("Network.clearBrowserCookies")

    def set_user_agent(self, user_agent: str, *, accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8",
                       platform: str = "Win32", metadata: dict | None = None) -> None:
        params: dict[str, Any] = {"userAgent": user_agent, "acceptLanguage": accept_language,
                                  "platform": platform}
        if metadata:
            params["userAgentMetadata"] = metadata
        self.send("Network.setUserAgentOverride", params)

    def set_timezone(self, timezone_id: str) -> None:
        try:
            self.send("Emulation.setTimezoneOverride", {"timezoneId": timezone_id})
        except CDPError:
            # 有些 Chrome 版本对空字符串/非法时区报错，失败不该让任务整体挂掉
            pass

    def set_locale(self, locale: str = "zh-CN") -> None:
        try:
            self.send("Emulation.setLocaleOverride", {"locale": locale})
        except CDPError:
            pass

    def set_viewport(self, width: int, height: int, *, scale: float = 1.0) -> None:
        try:
            self.send("Emulation.setDeviceMetricsOverride", {
                "width": int(width), "height": int(height), "deviceScaleFactor": float(scale),
                "mobile": False,
            })
        except CDPError:
            pass

    # ---- 输入 ----
    def capture_screenshot(self, *, format: str = "png", quality: int | None = None,
                           clip: dict | None = None, beyond_viewport: bool | None = None,
                           timeout: float | None = None) -> str:
        """截图，返回 base64（不带 ``data:`` 前缀）。"""
        params: dict[str, Any] = {"format": format}
        if format == "jpeg" and quality:
            params["quality"] = int(quality)
        if clip:
            params["clip"] = {
                "x": max(0.0, float(clip.get("x", 0))),
                "y": max(0.0, float(clip.get("y", 0))),
                "width": max(1.0, float(clip.get("width", 1))),
                "height": max(1.0, float(clip.get("height", 1))),
                "scale": float(clip.get("scale", 1)),
            }
            params["captureBeyondViewport"] = True if beyond_viewport is None else bool(beyond_viewport)
        elif beyond_viewport:
            params["captureBeyondViewport"] = True
        result = self.send("Page.captureScreenshot", params, timeout=timeout)
        return str(result.get("data") or "")

    def screenshot_data_url(self, *, format: str = "png", clip: dict | None = None) -> str:
        data = self.capture_screenshot(format=format, clip=clip)
        return f"data:image/{format};base64,{data}" if data else ""

    def dispatch_mouse_event(self, event_type: str, x: float, y: float, *, button: str = "left",
                             buttons: int = 0, click_count: int = 0) -> None:
        self.send("Input.dispatchMouseEvent", {
            "type": event_type, "x": float(x), "y": float(y), "button": button,
            "buttons": int(buttons), "clickCount": int(click_count),
        })

    def move_mouse(self, x: float, y: float, *, steps: int = 1) -> None:
        """分步移动鼠标（steps>1 时产生轨迹，更像真人）。"""
        if steps <= 1:
            self.dispatch_mouse_event("mouseMoved", x, y)
            return
        start = getattr(self, "_last_mouse", None) or (x, y)
        for px, py in humanize.mouse_path(start, (x, y), steps):
            self.dispatch_mouse_event("mouseMoved", px, py)
        self._last_mouse = (x, y)

    def click(self, x: float, y: float, *, button: str = "left", click_count: int = 1) -> None:
        self.move_mouse(x, y)
        self.dispatch_mouse_event("mousePressed", x, y, button=button, buttons=1,
                                  click_count=click_count)
        self.dispatch_mouse_event("mouseReleased", x, y, button=button, buttons=0,
                                  click_count=click_count)

    def dispatch_key_event(self, event_type: str, spec: KeySpec, *,
                           modifiers: int = 0) -> None:
        params: dict[str, Any] = {
            "type": event_type,
            "key": spec.key,
            "code": spec.code,
            "windowsVirtualKeyCode": spec.vk,
            "nativeVirtualKeyCode": spec.vk,
            "modifiers": modifiers,
        }
        if spec.text and event_type == "keyDown":
            # 只有 keyDown 带 text 才会真正产生字符；keyUp 带 text 会重复输入
            params["text"] = spec.text
        if spec.text and event_type == "char":
            params["text"] = spec.text
        self.send("Input.dispatchKeyEvent", params)

    def press_key(self, key: str, *, modifiers: int = 0) -> None:
        spec = KEYMAP.get(str(key).lower())
        if spec is None:
            if len(str(key)) == 1:
                spec = KeySpec(str(key), "", 0, str(key))
            else:
                raise CDPError(f"不认识的按键: {key!r}")
        self.dispatch_key_event("keyDown", spec, modifiers=modifiers)
        self.dispatch_key_event("keyUp", spec, modifiers=modifiers)

    def select_all(self) -> None:
        """全选当前焦点元素的内容（`commands=["selectAll"]` 由浏览器执行）。

        为什么不用 `Ctrl+A` 键盘事件：`Input.dispatchKeyEvent` 需要完整的
        `code` / `windowsVirtualKeyCode`，只给 `key='a'` 时 Chromium 不认，
        全选静默失效（实测：清空编辑器失败，残留文字会混进下一条消息）。
        """
        payload = {
            "type": "keyDown", "modifiers": 2,
            "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
            "commands": ["selectAll"],
        }
        self.send("Input.dispatchKeyEvent", payload)
        self.send("Input.dispatchKeyEvent", {**payload, "type": "keyUp", "commands": []})

    def type_text(self, text: str, *, delay: float = 0.0) -> None:
        """逐字输入（走真实键盘事件，Draft.js 能收到）。"""
        for char in str(text):
            self.dispatch_key_event("keyDown", KeySpec(char, "", 0, char))
            self.dispatch_key_event("keyUp", KeySpec(char, "", 0, char))
            if delay > 0:
                time.sleep(delay)

    def insert_text(self, text: str) -> None:
        """``Input.insertText``：一次性插入文本（不产生 keydown，但会触发 input）。"""
        self.send("Input.insertText", {"text": str(text)})

    # ---- 关闭 ----
    def close(self) -> None:
        if self.session_id:
            try:
                self.send("Target.detachFromTarget", {"sessionId": self.session_id},
                          timeout=2.0)
            except CDPError:
                pass
            return
        if self._owns:
            self._conn.close()

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def clean_cookie(cookie: dict) -> dict:
    """挑出 ``Network.setCookie(s)`` 认识的字段。

    ``Network.getCookies`` 返回的 cookie 带 ``size/session/priority/...`` 等只读
    元信息，原样回灌在部分 Chrome 版本上会报 ``Invalid cookie fields``。
    """
    keep = ("name", "value", "domain", "path", "expires", "httpOnly", "secure",
            "sameSite", "url", "sameParty", "sourceScheme", "sourcePort")
    out: dict[str, Any] = {}
    for key in keep:
        if key not in cookie:
            continue
        value = cookie[key]
        if key == "sameSite":
            value = str(value)
            if value.lower() not in ("strict", "lax", "none"):
                continue
            value = value.capitalize()
        if key == "expires" and value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
        out[key] = value
    return out


# --------------------------------------------------------------------------
# 附加 / 新开页面
# --------------------------------------------------------------------------
def list_targets(port: int, *, timeout: float = HTTP_TIMEOUT) -> list[dict]:
    return [t for t in (http_json(f"http://127.0.0.1:{int(port)}/json/list", timeout=timeout) or [])
            if isinstance(t, dict)]


def create_page(chrome_process: ChromeProcess | int, url: str = "about:blank", *,
                timeout: float = 15.0, default_timeout: float = DEFAULT_TIMEOUT) -> CDPSession:
    """新开一个标签页并返回连到它的 :class:`CDPSession`。

    实现上先用浏览器级端点 ``Target.createTarget``，再通过 ``/json/list`` 找到
    这个 target 自己的 WS 地址另开一条连接。多花一次握手，换来不用处理
    flatten 会话的 ``sessionId`` 路由，稳定得多（CDP 的 flatten 会话在
    浏览器崩溃恢复后容易错乱）。
    """
    port = chrome_process.port if isinstance(chrome_process, ChromeProcess) else int(chrome_process)
    browser_ws = (chrome_process.browser_ws_url(timeout) if isinstance(chrome_process, ChromeProcess)
                  else f"ws://127.0.0.1:{port}/devtools/browser")
    browser_session = CDPSession.connect(browser_ws, timeout=timeout, default_timeout=timeout)
    try:
        created = browser_session.new_target(url)
        target_id = str(created.get("targetId") or "")
    finally:
        browser_session.close()
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        for target in list_targets(port, timeout=3.0):
            if target.get("type") != "page":
                continue
            if target_id and target.get("id") != target_id:
                continue
            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            if ws_url:
                return CDPSession.connect(ws_url, timeout=timeout,
                                          default_timeout=default_timeout,
                                          target_id=target_id or str(target.get("id") or ""))
        time.sleep(0.2)
    raise CDPTimeout("新标签页没有在超时时间内出现在 /json/list 里")


def attach(target: Any = None, *, settings: Any = None, chrome_process: ChromeProcess | None = None,
           port: int | None = None, timeout: float = 15.0,
           default_timeout: float = DEFAULT_TIMEOUT) -> CDPSession:
    """连到已有页面。

    ``target`` 可以是：

    * 已经是 :class:`CDPSession` → 原样返回
    * ``ws://`` / ``wss://`` 地址 → 直接连
    * ``/json/list`` 里的一项（dict，带 ``webSocketDebuggerUrl``）→ 连它
    * 一个 port 数字 → 取该端口下第一个 page target
    * ``None`` → 用 ``chrome_process`` 或 ``settings``+port 推断
    """
    if isinstance(target, CDPSession):
        return target
    if isinstance(target, dict):
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise CDPError("target 字典里没有 webSocketDebuggerUrl")
        return CDPSession.connect(ws_url, timeout=timeout, default_timeout=default_timeout,
                                  target_id=str(target.get("id") or ""))
    if isinstance(target, str) and target.startswith(("ws://", "wss://")):
        return CDPSession.connect(target, timeout=timeout, default_timeout=default_timeout)
    if isinstance(target, ChromeProcess):
        chrome_process = target
        target = None
    if isinstance(target, (int, str)) and str(target).isdigit():
        port = int(target)
        target = None
    if port is None and chrome_process is not None:
        port = chrome_process.port
    if port is None and settings is not None:
        raise CDPError(
            "attach 需要一个端口或 ChromeProcess：请先用 launch_chrome() 启动浏览器"
        )
    if port is None:
        raise CDPError("attach 缺少目标：请传 ws 地址 / target 字典 / 端口 / ChromeProcess")

    pages = [t for t in list_targets(port, timeout=timeout) if t.get("type") == "page"]
    if not pages:
        if chrome_process is not None:
            return create_page(chrome_process, "about:blank", timeout=timeout,
                               default_timeout=default_timeout)
        raise CDPError(f"端口 {port} 下没有 page 目标")
    # 优先选抖音页面（多标签时不会连错）
    pages.sort(key=lambda t: 0 if "douyin.com" in str(t.get("url") or "") else 1)
    chosen = pages[0]
    ws_url = str(chosen.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        raise CDPError(f"target 没有 webSocketDebuggerUrl: {chosen.get('id')}")
    return CDPSession.connect(ws_url, timeout=timeout, default_timeout=default_timeout,
                              target_id=str(chosen.get("id") or ""))


def attach_proxy_auth(session: CDPSession, proxy: Any) -> bool:
    """让带账号密码的代理能通过认证。

    原理：开启 ``Fetch`` 域并拦截 ``Fetch.authRequired`` 事件，按配置里的
    用户名密码应答 ``Fetch.continueWithAuth``。

    **本机未验证**（没有可用代理），置信度：中——事件名与参数取自 CDP 官方
    ``Fetch`` 域文档，但不同 Chrome 版本对 ``proxyCredentials`` 的支持有差异。
    """
    if not isinstance(proxy, dict):
        return False
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    if not username:
        return False

    def handler(message: dict) -> None:                          # pragma: no cover - 需真代理
        params = message.get("params") or {}
        request_id = str(params.get("requestId") or "")
        if not request_id:
            return
        try:
            if str(params.get("authChallenge") or {}).get("source") == "Proxy":
                session.send("Fetch.continueWithAuth", {
                    "requestId": request_id,
                    "authChallengeResponse": {
                        "response": "ProvideCredentials",
                        "username": username,
                        "password": password,
                    },
                })
            else:
                session.send("Fetch.continueWithAuth", {
                    "requestId": request_id,
                    "authChallengeResponse": {"response": "Default"},
                })
        except CDPError:
            pass

    session.send("Fetch.enable", {"handleAuthRequests": True,
                                  "patterns": [{"urlPattern": "*"}]})
    session.on("Fetch.authRequired", handler)
    return True


def browser_health(settings: Any) -> dict:
    """环境体检的一部分：浏览器是否可用（``main.py check`` 也用）。"""
    path = chrome_mod.find_browser(getattr(settings, "chrome_path", "") or "")
    return {
        "browser_found": bool(path),
        "browser_path": path,
        "browser_kind": chrome_mod.browser_kind(path) if path else "",
        "headless": bool(getattr(settings, "browser_headless", True)),
        "demo_mode": bool(getattr(settings, "demo_mode", False)),
        "disk_free_gb": _disk_free_gb(settings),
    }


def _disk_free_gb(settings: Any) -> float:
    try:
        usage = shutil.disk_usage(str(getattr(settings, "data_dir", ".")))
        return round(usage.free / (1024 ** 3), 1)
    except OSError:                                             # pragma: no cover
        return 0.0
