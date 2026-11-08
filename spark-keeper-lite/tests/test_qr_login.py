"""「抖音扫码没反应」回归测试（交付版，自包含、不依赖任何测试夹具）。

为什么单独一个文件
------------------
2026-10 用户报「抖音扫码后没有反应」。实机排查出三个真问题，任何一个复发都会
让扫码功能再次失效，但它们都**不是**「起浏览器才测得出」的问题，完全可以在这里锁死：

1. 选择器失效：抖音登录弹层是哈希类名，只有 ``#animate_qrcode_container`` 等少数
   id 稳定。旧选择器（``[data-e2e="login-panel"]`` 等）实测命中 0，弹层打不开。
2. 截错区域：弹层打开后二维码位置先是**抖音 logo 占位 SVG**，约 3 秒后才换成真
   二维码。旧实现拿不到图片就回落到「二维码容器」（实际是整个登录面板 726×483），
   结果给用户看的是一张**没有二维码的面板截图** —— 手机怎么扫都没反应。
3. 判错成功：扫码确认后页面刷新，DOM 头像读不到；改用会话 cookie（``sessionid``）
   判定。但 ``passport_csrf_token`` 在**打开弹层时就下发**（未登录也有），误把它
   当登录信号会导致「没扫码却报登录成功」。

本文件只做**纯逻辑断言**（不启动浏览器、不连网），可在任何机器/CI 上稳定运行。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.automation import douyin, login, selectors


class _FakeCDP:
    """最小 CDP 替身：只实现被测试代码用到的三个方法。"""

    def __init__(self, find_result=None, cookies=None):
        self.find_result = find_result if find_result is not None else {"found": False}
        self.cookies = cookies or []
        self.clips: list = []

    def call(self, js, payload=None, timeout=None):        # noqa: ARG002
        return self.find_result

    def capture_screenshot(self, format="png", clip=None, **kwargs):   # noqa: A002,ARG002
        self.clips.append(clip)
        return "aGVsbG8="

    def get_cookies(self):
        return self.cookies


class QrSelectorTests(unittest.TestCase):
    """选择器必须包含实机校准出来的稳定 id。"""

    def test_container_uses_real_douyin_ids(self):
        sels = selectors.GROUPS["login_qrcode_container"].selectors
        for need in ("#animate_qrcode_container",
                     "#douyin_login_comp_scan_code",
                     "article#douyin_login_comp_flat_panel"):
            self.assertIn(need, sels, f"登录弹层选择器缺少实机验证过的 {need}")

    def test_image_only_matches_real_qr(self):
        sels = selectors.GROUPS["login_qrcode_image"].selectors
        self.assertIn('#animate_qrcode_container img[src^="data:image"]', sels)
        # 关键：不能有裸 canvas —— 页面上的广告 canvas(300×150) 会被当成二维码截给用户
        self.assertNotIn("canvas", sels)

    def test_login_required_recognises_login_panel(self):
        sels = selectors.GROUPS["login_required"].selectors
        self.assertIn("article#douyin_login_comp_flat_panel", sels)


class CaptureQrcodeTests(unittest.TestCase):
    """只截真二维码：不回落面板、不截占位图。"""

    def test_missing_image_returns_empty_and_takes_no_screenshot(self):
        cdp = _FakeCDP(find_result={"found": False})
        self.assertEqual(login._capture_qrcode(cdp), "")        # noqa: SLF001
        self.assertEqual(cdp.clips, [], "没找到二维码时不应该截图")

    def test_small_placeholder_is_rejected(self):
        cdp = _FakeCDP(find_result={"found": True,
                                    "rect": {"x": 0, "y": 0, "width": 60, "height": 60}})
        self.assertEqual(login._capture_qrcode(cdp), "")        # noqa: SLF001
        self.assertEqual(cdp.clips, [])

    def test_real_qr_is_captured_at_its_own_rect(self):
        cdp = _FakeCDP(find_result={"found": True,
                                    "rect": {"x": 10, "y": 20, "width": 178, "height": 178}})
        out = login._capture_qrcode(cdp)                        # noqa: SLF001
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(len(cdp.clips), 1)
        self.assertEqual(cdp.clips[0]["width"], 178)
        self.assertEqual(cdp.clips[0]["height"], 178)


class LoginCookieTests(unittest.TestCase):
    """扫码确认后的「已登录」硬信号 = 会话 cookie。"""

    def test_accepts_real_session_cookie(self):
        cdp = _FakeCDP(cookies=[{"name": "ttwid", "value": "x"},
                                {"name": "sessionid", "value": "abc123"}])
        self.assertTrue(douyin.has_login_cookie(cdp))

    def test_ignores_passport_helpers(self):
        # 实测：打开登录弹层就会下发这些，未登录也有 → 不能当登录成功
        cdp = _FakeCDP(cookies=[
            {"name": "passport_csrf_token", "value": "t"},
            {"name": "passport_csrf_token_default", "value": "t"},
            {"name": "passport_auth_mix_state", "value": "t"},
            {"name": "ttwid", "value": "x"},
        ])
        self.assertFalse(douyin.has_login_cookie(cdp))

    def test_ignores_empty_cookie_value(self):
        cdp = _FakeCDP(cookies=[{"name": "sessionid", "value": ""}])
        self.assertFalse(douyin.has_login_cookie(cdp))

    def test_cookie_read_failure_is_not_fatal(self):
        class Broken(_FakeCDP):
            def get_cookies(self):
                raise RuntimeError("boom")
        self.assertFalse(douyin.has_login_cookie(Broken()))


class LoginPageStateTests(unittest.TestCase):
    """状态判定：有二维码=待扫码；有会话 cookie=已登录。"""

    def test_pending_when_qr_visible(self):
        def fake_find(cdp, name, **kwargs):                     # noqa: ARG001
            return {"found": True} if name == "login_qrcode_image" else None
        with mock.patch.object(douyin, "has_login_cookie", return_value=False), \
                mock.patch.object(selectors, "find_first", side_effect=fake_find), \
                mock.patch.object(selectors, "text_present", return_value=False):
            self.assertEqual(douyin.login_page_state(_FakeCDP()), "pending")

    def test_logged_in_when_session_cookie_present(self):
        with mock.patch.object(douyin, "has_login_cookie", return_value=True):
            self.assertEqual(douyin.login_page_state(_FakeCDP()), "logged_in")

    def test_scanned_when_confirm_text_visible(self):
        with mock.patch.object(douyin, "has_login_cookie", return_value=False), \
                mock.patch.object(selectors, "text_present", return_value=True):
            self.assertEqual(douyin.login_page_state(_FakeCDP()), "scanned")


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
