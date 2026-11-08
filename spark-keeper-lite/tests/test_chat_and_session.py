"""私信页地址 / 登录态恢复 / Cookie 导入 origin 的回归测试（交付版，自包含）。

三个真事故各一条，都是「看起来成功、实际发错页面或误判登录」的坑：

1. **发送续火花必须走 ``https://www.douyin.com/chat?isPopup=1``**。
   换地址 = 换页面结构，会话列表/输入框可能都不在，消息要么发不出、要么发到别处。
2. **恢复登录态必须导航**。早期 ``restore_state`` 在 ``origins`` 为空时直接 return，
   浏览器停在 ``about:blank``，于是导入的 Cookie 一律被判「抖音未接受」——
   实测用户拿一份好 Cookie 白折腾一轮。
3. **导入 Cookie 要记录主站 origin**，否则会话文件不自描述，恢复时无从判断去哪。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.automation import douyin, session_store


class _FakeCDP:
    """只实现恢复登录态 / 打开私信页用到的 CDP 方法。

    ``redirect_to`` 用来模拟「抖音把页面重定向走」：navigate 之后 current_url
    返回它而不是导航目标。
    """

    def __init__(self, url: str = "about:blank", redirect_to: str = ""):
        self.cookies: list = []
        self.navigations: list = []
        self._url = url
        self.redirect_to = redirect_to

    def set_cookies(self, cookies):
        self.cookies = list(cookies)
        return len(self.cookies)

    def navigate(self, url, timeout=None, wait_ready=True):     # noqa: ARG002
        self.navigations.append(url)
        self._url = self.redirect_to or url

    def current_url(self):
        return self._url

    def evaluate(self, expression, timeout=None):               # noqa: ARG002
        return "0"

    def call(self, js, payload=None, timeout=None):             # noqa: ARG002
        return {"found": True}


class ChatUrlTests(unittest.TestCase):
    def test_chat_url_is_the_required_one(self):
        # 用户明确要求：发送续火花时网址必须是这个
        self.assertEqual(douyin.CHAT_URL, "https://www.douyin.com/chat?isPopup=1")

    def test_message_urls_put_chat_url_first(self):
        self.assertEqual(douyin.MESSAGE_URLS[0], douyin.CHAT_URL)

    def test_is_chat_url_accepts_chat_with_or_without_query(self):
        """私信页两种形态都算合法。

        历史实现要求必须带 ``isPopup=1``，但实测抖音经常把 query 丢掉、只留
        ``https://www.douyin.com/chat``。真实事故：任务报
        「等待会话列表超时（当前页面：https://www.douyin.com/chat）」
        —— 其实页面就是私信页，是判定把自己坑了。
        """
        self.assertTrue(douyin.is_chat_url(douyin.CHAT_URL))
        self.assertTrue(douyin.is_chat_url("https://www.douyin.com/chat?isPopup=1&x=2"))
        self.assertTrue(douyin.is_chat_url("https://www.douyin.com/chat"))
        self.assertTrue(douyin.is_chat_url("https://www.douyin.com/chat/"))
        self.assertFalse(douyin.is_chat_url("https://www.douyin.com/jingxuan"))
        self.assertFalse(douyin.is_chat_url("https://www.douyin.com/chatx"))
        self.assertFalse(douyin.is_chat_url(""))

    def test_open_chat_navigates_to_chat_url_only(self):
        cdp = _FakeCDP()
        with mock.patch.object(douyin.selectors, "find_first", return_value={"found": True}):
            result = douyin.open_chat(cdp)
        self.assertEqual(cdp.navigations, [douyin.CHAT_URL],
                         "open_chat 只能导航到 CHAT_URL，不能试别的地址")
        self.assertTrue(result["ok"])
        self.assertTrue(result["canonical"])

    def test_open_chat_flags_redirect_as_not_canonical(self):
        # 抖音把页面重定向到首页（典型：登录态失效）
        cdp = _FakeCDP(redirect_to="https://www.douyin.com/jingxuan")
        with mock.patch.object(douyin.selectors, "find_first", return_value={"found": True}):
            result = douyin.open_chat(cdp)
        self.assertEqual(cdp.navigations, [douyin.CHAT_URL])
        self.assertTrue(result["ok"])
        self.assertFalse(result["canonical"], "被重定向时必须标记 canonical=False")


class RestoreStateTests(unittest.TestCase):
    def test_restore_navigates_to_main_origin_without_origins(self):
        """没有 origins（导入的 Cookie 就是这种）时也必须导航到抖音主站。"""
        cdp = _FakeCDP()
        result = session_store.restore_state(
            cdp, {"cookies": [{"name": "sessionid", "value": "x"}], "origins": {}})
        self.assertEqual(cdp.navigations, [session_store.MAIN_ORIGIN])
        self.assertTrue(result["defaulted_url"])

    def test_restore_uses_saved_origin_when_present(self):
        cdp = _FakeCDP()
        session_store.restore_state(
            cdp, {"cookies": [], "origins": {"https://www.douyin.com": {}}})
        self.assertEqual(cdp.navigations, ["https://www.douyin.com"])

    def test_restore_respects_explicit_url(self):
        cdp = _FakeCDP()
        session_store.restore_state(
            cdp, {"cookies": [], "origins": {}}, url=douyin.CHAT_URL)
        self.assertEqual(cdp.navigations, [douyin.CHAT_URL])

    def test_restore_no_navigate_flag(self):
        cdp = _FakeCDP()
        session_store.restore_state(cdp, {"cookies": [], "origins": {}}, navigate=False)
        self.assertEqual(cdp.navigations, [])


class ImportCookieOriginsTests(unittest.TestCase):
    def test_imported_state_records_main_origin(self):
        settings = mock.Mock()
        settings.data_dir = "/tmp/sk-test-sessions"     # 仅用于 session_dir 拼接
        with mock.patch.object(session_store, "save_state") as save:
            save.return_value = type("P", (), {"name": "account_9.json"})()
            result = session_store.import_cookies(
                {"id": 9}, "sessionid=abc123; ttwid=xyz", settings)
        self.assertTrue(result["ok"])
        saved = save.call_args[0][1]
        self.assertIn(session_store.MAIN_ORIGIN, saved["origins"],
                      "导入的登录态必须记录主站 origin，否则恢复时不知道该导航到哪")

    def test_import_rejects_without_session_cookie(self):
        settings = mock.Mock()
        settings.data_dir = "/tmp/sk-test-sessions"
        with mock.patch.object(session_store, "save_state") as save:
            result = session_store.import_cookies({"id": 9}, "ttwid=xyz", settings)
        self.assertFalse(result["ok"])
        save.assert_not_called()


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
