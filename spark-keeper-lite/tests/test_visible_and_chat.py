"""云服务器上的两个真实故障的回归测试。

1. **可见登录在无图形界面的服务器上必然失败**
       Missing X server or $DISPLAY / The platform failed to initialize. Exiting.
   → 现在 `display_available()` 先探测 DISPLAY，没有就自动降级成无头（网页扫码）。

2. **任务报 `page_unavailable: 等待会话列表超时（当前页面：https://www.douyin.com/chat）`**
   抖音把 `?isPopup=1` 吃掉了，而旧 `is_chat_url()` 硬性要求带这个参数，
   于是判定「不是私信页」→ 等到超时。现在只看路径，并补一次导航/整页重载。
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from app.automation import cdp, douyin, session_store


class IsChatUrlTests(unittest.TestCase):
    def test_accepts_canonical_with_query(self):
        self.assertTrue(douyin.is_chat_url("https://www.douyin.com/chat?isPopup=1"))

    def test_accepts_plain_chat(self):
        # 真实事故现场就是这个地址
        self.assertTrue(douyin.is_chat_url("https://www.douyin.com/chat"))

    def test_accepts_trailing_slash_and_fragment(self):
        self.assertTrue(douyin.is_chat_url("https://www.douyin.com/chat/"))
        self.assertTrue(douyin.is_chat_url("https://www.douyin.com/chat#/x"))

    def test_rejects_other_pages(self):
        for url in ("https://www.douyin.com/", "", "https://www.douyin.com/user/abc",
                    "https://www.douyin.com/chatx"):
            self.assertFalse(douyin.is_chat_url(url), msg=url)


class DisplayAvailableTests(unittest.TestCase):
    def _call(self, *, windows: bool, env: dict):
        # display_available 的唯一实现在 cdp（session_store 只做委托），
        # 所以打桩 cdp._is_windows 才有效。
        with mock.patch.object(cdp, "_is_windows", return_value=windows), \
                mock.patch.object(cdp.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, env, clear=True):
            return session_store.display_available()

    def test_windows_is_true(self):
        self.assertTrue(self._call(windows=True, env={}))

    def test_x11_display(self):
        self.assertTrue(self._call(windows=False, env={"DISPLAY": ":0"}))

    def test_wayland_display(self):
        self.assertTrue(self._call(windows=False, env={"WAYLAND_DISPLAY": "wayland-0"}))

    def test_headless_server(self):
        # 没有 DISPLAY / WAYLAND_DISPLAY → 无图形界面
        self.assertFalse(self._call(windows=False, env={}))


class VisibleDowngradeTests(unittest.TestCase):
    """要求可见窗口但没有图形界面 → 必须降级成无头，而不是让 Chromium 直接退出。"""

    def _browser(self, *, headless, display: bool):
        browser = session_store.AccountBrowser.__new__(session_store.AccountBrowser)
        browser.headless = headless
        browser.headless_forced_reason = ""
        browser.settings = SimpleNamespace(browser_headless=True)
        with mock.patch.object(session_store, "_is_windows", return_value=False), \
                mock.patch.object(session_store, "display_available", return_value=display):
            browser._downgrade_visible_if_no_display()      # noqa: SLF001
        return browser

    def test_downgrades_when_no_display(self):
        browser = self._browser(headless=False, display=False)
        self.assertIsNot(browser.headless, False, "不能继续用可见模式")
        self.assertIn("图形界面", browser.headless_forced_reason)

    def test_keeps_visible_when_display_present(self):
        browser = self._browser(headless=False, display=True)
        self.assertIs(browser.headless, False, "有显示器时应保留可见窗口")
        self.assertEqual(browser.headless_forced_reason, "")

    def test_headless_request_untouched(self):
        browser = self._browser(headless=None, display=False)
        self.assertIsNone(browser.headless)
        self.assertEqual(browser.headless_forced_reason, "")


class LoginWallTests(unittest.TestCase):
    def _cdp(self, url: str, text: str = ""):
        return SimpleNamespace(
            current_url=lambda: url,
            evaluate=lambda expr: text,
        )

    def test_passport_redirect(self):
        reason = douyin._login_wall(self._cdp("https://passport.douyin.com/login?x=1"))  # noqa: SLF001
        self.assertIn("登录", reason)

    def test_login_modal(self):
        reason = douyin._login_wall(self._cdp("https://www.douyin.com/chat", "扫码登录"))  # noqa: SLF001
        self.assertIn("登录", reason)

    def test_normal_page(self):
        reason = douyin._login_wall(  # noqa: SLF001
            self._cdp("https://www.douyin.com/chat", "李森桦 发送消息"))
        self.assertEqual(reason, "")


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main(verbosity=2)
