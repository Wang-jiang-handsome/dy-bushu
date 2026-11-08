"""手动导入 Cookie 的回归测试（交付版，自包含）。

背景
----
用户报「扫码超时、二维码一直不出现」。除了修选择器，还提供一条**不依赖任何
页面结构**的兜底路径：用户在自己浏览器里登录好抖音，把 Cookie 粘进系统。

这里锁死四件事：

1. 五种粘贴格式都能解析（Cookie 头 / JSON 数组 / JSON 对象 / Netscape / 两列）；
2. **没有 sessionid 的 Cookie 必须拒绝**（否则会存进一个假登录态，任务全失败）；
3. 导入结果**不回显 cookie 值**（等价于密码，不能进日志/响应）；
4. 归一化后的 cookie 带 domain/path/httpOnly，能被 ``Network.setCookies`` 直接吃。
"""

from __future__ import annotations

import json
import unittest

from app.automation import session_store


class ParseCookieTextTests(unittest.TestCase):
    def test_cookie_header_format(self):
        cookies = session_store.parse_cookie_text("sessionid=abc123; sid_guard=def456; ttwid=xyz")
        names = [c["name"] for c in cookies]
        self.assertEqual(names, ["sessionid", "sid_guard", "ttwid"])
        self.assertEqual(cookies[0]["value"], "abc123")

    def test_cookie_header_with_prefix(self):
        cookies = session_store.parse_cookie_text("Cookie: sessionid=abc123; ttwid=xyz")
        self.assertEqual([c["name"] for c in cookies], ["sessionid", "ttwid"])

    def test_json_array_format(self):
        raw = json.dumps([{
            "name": "sessionid", "value": "abc123", "domain": ".douyin.com",
            "path": "/", "httpOnly": True, "secure": True, "expirationDate": 1893456000,
        }])
        cookies = session_store.parse_cookie_text(raw)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "sessionid")
        self.assertEqual(cookies[0]["expires"], 1893456000)
        self.assertTrue(cookies[0]["httpOnly"])

    def test_json_object_format(self):
        cookies = session_store.parse_cookie_text('{"sessionid": "abc123", "ttwid": "xyz"}')
        self.assertEqual([c["name"] for c in cookies], ["sessionid", "ttwid"])

    def test_netscape_format(self):
        raw = ".douyin.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc123"
        cookies = session_store.parse_cookie_text(raw)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "sessionid")
        self.assertEqual(cookies[0]["domain"], ".douyin.com")
        self.assertTrue(cookies[0]["secure"])

    def test_two_column_format(self):
        cookies = session_store.parse_cookie_text("sessionid\tabc123")
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["value"], "abc123")

    def test_quoted_value_is_unquoted(self):
        cookies = session_store.parse_cookie_text('sessionid="abc123"')
        self.assertEqual(cookies[0]["value"], "abc123")

    def test_garbage_is_ignored(self):
        cookies = session_store.parse_cookie_text("这不是 cookie\n\n=  = ==\n")
        self.assertEqual(cookies, [])

    def test_defaults_are_douyin_friendly(self):
        cookie = session_store.parse_cookie_text("sessionid=abc")[0]
        self.assertEqual(cookie["domain"], ".douyin.com")
        self.assertEqual(cookie["path"], "/")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httpOnly"], "sessionid 是 httpOnly，不设会丢登录态")
        self.assertEqual(cookie["expires"], 0)      # 会话 cookie


class SummarizeTests(unittest.TestCase):
    def test_detects_login_cookie(self):
        cookies = session_store.parse_cookie_text("ttwid=x; sessionid=abc")
        summary = session_store.summarize_cookies(cookies)
        self.assertTrue(summary["has_login_cookie"])
        self.assertEqual(summary["login_cookie_name"], "sessionid")
        self.assertEqual(summary["cookie_count"], 2)

    def test_passport_helpers_are_not_login_signals(self):
        # 打开登录弹层就会下发这些，未登录也有 → 不能当作登录成功
        cookies = session_store.parse_cookie_text(
            "passport_csrf_token=t; passport_auth_mix_state=t; ttwid=x")
        self.assertFalse(session_store.summarize_cookies(cookies)["has_login_cookie"])

    def test_summary_never_leaks_values(self):
        cookies = session_store.parse_cookie_text("sessionid=SUPER_SECRET_VALUE")
        summary = session_store.summarize_cookies(cookies)
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("SUPER_SECRET_VALUE", blob)
        self.assertIn("sessionid", summary["names"])


class LoginCookieNamesTests(unittest.TestCase):
    def test_passport_names_are_excluded(self):
        for name in ("passport_csrf_token", "passport_auth_mix_state",
                     "passport_csrf_token_default"):
            self.assertNotIn(name, session_store.LOGIN_COOKIE_NAMES)

    def test_session_names_are_included(self):
        for name in ("sessionid", "sid_guard", "sid_tt"):
            self.assertIn(name, session_store.LOGIN_COOKIE_NAMES)


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
