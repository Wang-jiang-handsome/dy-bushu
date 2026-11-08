"""Chrome / Edge 可执行文件探测。

自托管与容器两种场景都要能跑，所以按「显式配置 → 常见发行版 → PATH」的顺序找，
并把结果缓存起来（每次任务执行都调用它）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_CACHE: dict[str, str | None] = {}

LINUX_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/opt/google/chrome/chrome",
    "/snap/bin/chromium",
)
MAC_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
WINDOWS_CANDIDATES = (
    # ---- Edge 优先（用户要求：本机窗口登录优先用 Edge）----
    # Edge 是所有 Windows 10/11 预装的，Chrome 不一定有；而且部分机器上
    # Chrome 是「按用户安装」（在 %LOCALAPPDATA%），全盘路径找不到。
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    # ---- 其次 Chrome ----
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
)
PATH_NAMES = (
    # 与上面一致：Edge 优先
    "msedge", "microsoft-edge", "msedge.exe",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "chrome", "chrome.exe",
)


def _candidates(explicit: str = "") -> list[str]:
    out: list[str] = []
    if explicit:
        out.append(explicit)
    env = os.environ.get("CHROME_PATH") or os.environ.get("SK_CHROME_PATH")
    if env:
        out.append(env)
    if sys.platform.startswith("win"):
        out.extend(WINDOWS_CANDIDATES)
        for extra in (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",):
            out.append(extra)
    elif sys.platform == "darwin":
        out.extend(MAC_CANDIDATES)
    else:
        out.extend(LINUX_CANDIDATES)
    return out


def find_browser(explicit: str = "") -> str:
    """返回可用的浏览器可执行文件绝对路径；找不到返回空串。"""
    cache_key = explicit or "__auto__"
    if cache_key in _CACHE:
        return _CACHE[cache_key] or ""

    for candidate in _candidates(explicit):
        if not candidate:
            continue
        # 候选里可能有 %LOCALAPPDATA% 这类环境变量（按用户安装的 Edge/Chrome
        # 就装在用户目录下）：先展开再判断，否则永远找不到。
        expanded = os.path.expandvars(candidate)
        if "%" in expanded:          # 变量没定义：跳过，避免把字面量当路径
            continue
        path = Path(expanded).expanduser()
        if path.is_file() and os.access(str(path), os.X_OK if os.name != "nt" else os.F_OK):
            resolved = str(path.resolve())
            _CACHE[cache_key] = resolved
            return resolved
    for name in PATH_NAMES:
        found = shutil.which(name)
        if found:
            _CACHE[cache_key] = found
            return found
    _CACHE[cache_key] = None
    return ""


def browser_kind(path: str) -> str:
    name = (path or "").lower()
    if "msedge" in name or "edge" in name:
        return "edge"
    if "chromium" in name and "chrome" not in name:
        return "chromium"
    if "chrome" in name:
        return "chrome"
    return "unknown"


def is_available(explicit: str = "") -> bool:
    return bool(find_browser(explicit))


def reset_cache() -> None:
    _CACHE.clear()


def describe(explicit: str = "") -> dict:
    path = find_browser(explicit)
    return {
        "found": bool(path),
        "path": path,
        "kind": browser_kind(path) if path else "",
        "headless_supported": bool(path),
    }
