"""拟人化：延时、逐字输入、鼠标轨迹、UA / 时区 / 语言。

为什么需要
----------
抖音的私信页对「机器人特征」有风控：固定间隔、瞬间填入大段文字、无鼠标移动
都会提高被判定为自动化的概率。这里把「像人」的参数集中到一处，方便调。

可测性
------
所有取值函数都接受 ``rng``（``random.Random``）并**保证落在边界内**，
纯函数不依赖浏览器，所以本机能完整单测（含 min>max、负数、空文本等畸形输入）。

不确定项
--------
UA 池是**固定字符串**（不做版本伪装拼接）：伪造成不存在的 Chrome 版本反而更
可疑。真实风控策略无法在本机验证，置信度：中。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Iterable, Sequence

#: 常见桌面 Chrome UA（Windows / macOS）。刻意只保留真实存在过的组合：
#: 「Chrome 版本号 + 对应平台」不一致是比自动化更明显的特征。
UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
)

ACCEPT_LANGUAGES: tuple[str, ...] = (
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh-Hans;q=0.9,en;q=0.8",
)

#: 平台 → (accept-language, platform 值, navigator.platform)
PLATFORM_HINTS: dict[str, tuple[str, str, str]] = {
    "windows": ("zh-CN,zh;q=0.9,en;q=0.8", "Win32", "Win32"),
    "mac": ("zh-CN,zh;q=0.9,en;q=0.8", "MacIntel", "MacIntel"),
}


def make_rng(seed: int | None = None) -> random.Random:
    """统一入口：测试里传固定种子，生产里传 None（用系统熵）。"""
    return random.Random(seed) if seed is not None else random.Random()


def clamp(value: float, low: float, high: float) -> float:
    """把 value 夹到 [low, high]；low > high 时先交换（畸形配置不能让任务崩）。"""
    low, high = (high, low) if low > high else (low, high)
    return max(low, min(high, value))


def bounds(value: Any, default: tuple[float, float] = (3.0, 9.0)) -> tuple[float, float]:
    """规范化「区间」配置。

    接受 ``{"min":3,"max":9}`` / ``[3, 9]`` / ``"3-9"`` / ``(3, 9)`` /
    ``'{"min":3,"max":9}'``（**数据库里 TEXT 列存的就是这种 JSON 字符串**）。
    非法输入回落默认值。返回 (low, high)，保证 ``low <= high`` 且 ``low >= 0``。

    ⚠️ 必须能解析 JSON 字符串：``scheduled_tasks.send_gap_seconds`` 是 TEXT 列，
    从库里读出来是字符串。早期版本没解析它，导致「配置的 0 秒间隔」被当成非法值
    回落到默认的 3~9 秒 —— 单测里表现为每个好友白等 3~9 秒。
    """
    low, high = default
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if isinstance(value, dict):
        low = _to_float(value.get("min"), low)
        high = _to_float(value.get("max"), high)
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        low = _to_float(value[0], low)
        high = _to_float(value[1], high)
    elif isinstance(value, str) and ("-" in value or "," in value):
        parts = value.replace("，", ",").replace("-", ",").split(",")
        if len(parts) >= 2:
            low = _to_float(parts[0], low)
            high = _to_float(parts[1], high)
    if low < 0:
        low = 0.0
    if high < low:
        # 配置写反了（min>max）时交换，而不是把 high 抬到 low：
        # 管理员想把「3~9 秒」写成 min=9/max=3 时，意图显然是 3~9。
        low, high = high, low
    return float(low), float(high)


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def random_between(low: float, high: float, rng: random.Random | None = None) -> float:
    """在 [low, high] 里取随机数（区间非法时返回 low）。"""
    low, high = (high, low) if low > high else (low, high)
    rng = rng or random
    if high <= low:
        return float(low)
    return rng.uniform(float(low), float(high))


def random_int(low: int, high: int, rng: random.Random | None = None) -> int:
    low, high = (high, low) if low > high else (low, high)
    rng = rng or random
    if high <= low:
        return int(low)
    return rng.randint(int(low), int(high))


def delay_seconds(bounds_value: Any = None, rng: random.Random | None = None,
                  default: tuple[float, float] = (3.0, 9.0)) -> float:
    """好友之间的等待秒数（``settings/send_gap_seconds`` 规范化后取值）。"""
    low, high = bounds(bounds_value, default)
    return round(random_between(low, high, rng), 2)


def sleep_gap(bounds_value: Any = None, rng: random.Random | None = None,
              default: tuple[float, float] = (3.0, 9.0)) -> float:
    """真的睡一觉，返回实际睡了多久（调用方写日志用）。"""
    seconds = delay_seconds(bounds_value, rng, default)
    if seconds > 0:
        time.sleep(seconds)
    return seconds


def typing_delay(char: str, rng: random.Random | None = None,
                 min_ms: int = 60, max_ms: int = 180) -> float:
    """单个字符的输入间隔（秒）。

    标点/emoji 之后多停一下，更像人（中文用户打完一个字常顿一下）。
    """
    rng = rng or random
    base = random_int(min_ms, max_ms, rng) / 1000.0
    if char and (char in "，。！？,.!?\n" or ord(char) > 0x1F000):
        base += random_between(0.05, 0.25, rng)
    return round(base, 3)


def typing_plan(text: str, rng: random.Random | None = None,
                min_ms: int = 60, max_ms: int = 180) -> list[tuple[str, float]]:
    """把文本拆成 ``[(字符, 该字之后等待秒数), ...]``，供 CDP 逐字输入用。"""
    rng = rng or random
    return [(char, typing_delay(char, rng, min_ms, max_ms)) for char in str(text or "")]


def total_typing_seconds(text: str, rng: random.Random | None = None,
                        min_ms: int = 60, max_ms: int = 180) -> float:
    """预演一遍输入耗时（用于任务超时预算，避免「还没输完就被判超时」）。"""
    return round(sum(delay for _c, delay in typing_plan(text, rng, min_ms, max_ms)), 3)


def mouse_path(start: Sequence[float], end: Sequence[float], steps: int = 8,
               rng: random.Random | None = None, jitter: float = 2.0) -> list[tuple[float, float]]:
    """生成一段带抖动的鼠标轨迹（线性插值 + 随机偏移）。

    不做贝塞尔曲线：CDP 只是把坐标转发给渲染进程，风控看的是「有没有轨迹、
    间隔是否均匀」，线性+抖动已经够用，而且可预测、好测试。
    """
    rng = rng or random
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    steps = max(1, int(steps))
    path: list[tuple[float, float]] = []
    for index in range(1, steps + 1):
        ratio = index / steps
        jitter_x = rng.uniform(-jitter, jitter) if index < steps else 0.0
        jitter_y = rng.uniform(-jitter, jitter) if index < steps else 0.0
        path.append((round(sx + (ex - sx) * ratio + jitter_x, 2),
                     round(sy + (ey - sy) * ratio + jitter_y, 2)))
    return path


def pick_user_agent(rng: random.Random | None = None, pool: Iterable[str] | None = None) -> str:
    rng = rng or random
    # 注意 ``pool or UA_POOL`` 会把「显式传空列表」当成没传：空池应该回落第一条，
    # 而不是随机挑一条（调用方传空池通常就是想固定 UA 做测试）。
    items = [ua for ua in (UA_POOL if pool is None else pool) if ua]
    if not items:
        return UA_POOL[0]
    return rng.choice(items)


def platform_of(user_agent: str) -> str:
    """从 UA 判断平台（用于设置 ``platform`` / ``navigator.platform``）。"""
    ua = (user_agent or "").lower()
    if "macintosh" in ua or "mac os x" in ua:
        return "mac"
    if "windows" in ua:
        return "windows"
    if "linux" in ua:
        return "linux"
    return "windows"


def ua_metadata(user_agent: str) -> dict:
    """``Network.setUserAgentOverride`` 的 ``userAgentMetadata``。

    不给它的话，``navigator.userAgentData`` 仍然是 HeadlessChrome —— 这是
    无头模式最容易被识别的地方之一。
    """
    ua = user_agent or UA_POOL[0]
    version = "131.0.0.0"
    marker = "Chrome/"
    if marker in ua:
        version = ua.split(marker, 1)[1].split(" ", 1)[0]
    platform = platform_of(ua)
    platform_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}.get(platform, "Windows")
    return {
        "brands": [
            {"brand": "Chromium", "version": version.split(".")[0]},
            {"brand": "Google Chrome", "version": version.split(".")[0]},
            {"brand": "Not_A Brand", "version": "24"},
        ],
        "fullVersion": version,
        "platform": platform_name,
        "platformVersion": {"windows": "10.0.0", "mac": "14.0.0", "linux": "6.1.0"}.get(
            platform, "10.0.0"),
        "architecture": "x86",
        "model": "",
        "mobile": False,
    }


def locale_profile(rng: random.Random | None = None) -> dict:
    """返回 ``{"user_agent","accept_language","platform","timezone"}`` 一组一致的身份。"""
    rng = rng or random
    user_agent = pick_user_agent(rng)
    platform = platform_of(user_agent)
    accept_language, platform_name, _nav = PLATFORM_HINTS.get(platform, PLATFORM_HINTS["windows"])
    if rng.random() < 0.5:
        accept_language = rng.choice(ACCEPT_LANGUAGES)
    return {
        "user_agent": user_agent,
        "accept_language": accept_language,
        "platform": platform_name,
        "timezone": "Asia/Shanghai",
        "metadata": ua_metadata(user_agent),
    }


def jitter_seconds(configured: int, rng: random.Random | None = None,
                   cap: int = 3600) -> int:
    """任务抖动：0~configured 秒之间随机。

    用「随机一次 + 记住」的策略（调用方用确定性种子），避免每次 tick 重新随机
    导致任务被无限推迟。``cap`` 防止管理员填了个巨大的值把任务推迟到第二天。
    """
    try:
        configured = int(configured or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured <= 0:
        return 0
    return random_int(0, min(configured, max(0, int(cap))), rng)


def reading_pause(text: str, rng: random.Random | None = None) -> float:
    """「看一眼再回复」的停顿：与文本长度弱相关，上限 3 秒。"""
    rng = rng or random
    length = len(str(text or ""))
    return round(clamp(0.6 + length * 0.05 + random_between(0, 0.8, rng), 0.4, 3.0), 2)


# --------------------------------------------------------------------------
# 反检测 / 身份伪装
# --------------------------------------------------------------------------
#: 注入到页面的 stealth 脚本（``Page.addScriptToEvaluateOnNewDocument`` 注册，
#: 每次新文档都会执行）。
#:
#: 来源：前作 ``douyin-spark-keeper/src/js_payloads.py`` 的 JS_STEALTH 思路 +
#: 公开的无头检测清单。置信度：中——抖音的风控策略会变，这里只是**降低明显特征**，
#: 不保证不被识别；真实效果只能在部署机观察。
JS_STEALTH = r"""
(() => {
  const define = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); }
    catch (e) { /* 某些属性不可重定义，忽略 */ }
  };
  define(navigator, 'webdriver', undefined);
  define(navigator, 'languages', ['zh-CN', 'zh', 'en']);
  define(navigator, 'hardwareConcurrency', 8);
  define(navigator, 'deviceMemory', 8);
  define(navigator, 'maxTouchPoints', 0);
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      define(navigator, 'plugins', [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
      ]);
      define(navigator, 'mimeTypes', [{ type: 'application/pdf' }]);
    }
  } catch (e) {}
  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  } catch (e) {}
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
  try {
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
    );
  } catch (e) {}
})();
"""


def apply_stealth(cdp: Any) -> bool:
    """把 stealth 脚本注册为「每个新文档都执行」。

    用 ``Page.addScriptToEvaluateOnNewDocument`` 而不是 ``Runtime.evaluate``：
    后者只作用于当前文档，页面一跳转就失效了。
    返回是否注册成功（失败不致命，只是检测特征多一点）。
    """
    try:
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": JS_STEALTH})
        return True
    except Exception:            # noqa: BLE001 - 反检测是尽力而为，不能让它把登录搞挂
        return False


def apply_identity(cdp: Any, rng: random.Random | None = None, *,
                   timezone_id: str = "Asia/Shanghai", locale: str = "zh-CN",
                   width: int | None = None, height: int | None = None) -> dict:
    """把一组一致的「身份」应用到浏览器（UA / 语言 / 时区 / 视口）。

    一致性比随机性更重要：UA 说 Windows 而 ``navigator.platform`` 是 Linux 之类
    的错配，比不做伪装更容易被识别。
    """
    profile = locale_profile(rng)
    cdp.set_user_agent(profile["user_agent"], accept_language=profile["accept_language"],
                       platform=profile["platform"], metadata=profile["metadata"])
    cdp.set_timezone(timezone_id)
    cdp.set_locale(locale)
    if width and height:
        cdp.set_viewport(int(width), int(height))
    apply_stealth(cdp)
    return profile
