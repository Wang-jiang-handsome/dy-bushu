"""发送链路回归：必须先「打开会话」，再输入文字（交付版，自包含）。

真实事故（用户截图）：好友结果全是 ``input_failed``——「四种输入方式都没能把
文字写进输入框」。

根因不是输入策略本身，而是**聊天面板根本没打开**：抖音私信页默认不选中任何
会话，右侧编辑器/发送按钮不渲染。旧实现 `harvest_conversations()` 读完列表就
直接 `send_message()` 往「输入框」写字，于是四种策略全部落空。

本文件锁死：
1. 选择器表里必须有 ``chat_header`` / ``chat_editor_area``（判断面板是否就绪）；
2. ``open_conversation`` 在「找不到会话」「标题对不上」时**明确失败**，
   绝不在错误对象上发消息；
3. ``open_conversation`` 在「会话已在当前面板」时直接返回，不重复点击；
4. 发起新登录前会取消该账号的旧会话（避免「浏览器并发已满」）。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.automation import douyin, login, selectors, task_runner


class _FakeCDP:
    """可编排的 CDP 替身：按调用顺序返回预设结果。"""

    def __init__(self, *, open_result=None, header="", editor_ready=True):
        self.open_result = open_result or {"found": False, "reason": "no_match"}
        self.header = header
        self.editor_ready = editor_ready
        self.calls: list[str] = []

    def call(self, js, payload=None, timeout=None):        # noqa: ARG002
        name = "open_conversation" if "want = String" in str(js) else "chat_header"
        self.calls.append(name)
        if name == "open_conversation":
            return self.open_result
        return {"found": bool(self.header), "text": self.header, "selector": "x"}

    def evaluate(self, expression, timeout=None):          # noqa: ARG002
        # _wait_conversation_rows 用它数会话行，返回正数表示「列表已渲染」
        return "6"

    def press_key(self, key, modifiers=0):                 # noqa: ARG002
        self.calls.append(f"key:{key}")

    def select_all(self):
        self.calls.append("select_all")

    def insert_text(self, text):                           # noqa: ARG002
        self.calls.append("insert_text")


class ChatSelectorTests(unittest.TestCase):
    def test_chat_groups_exist(self):
        for name in ("chat_header", "chat_editor_area"):
            self.assertIn(name, selectors.GROUPS,
                          f"缺少 {name} 选择器组（判断聊天面板是否就绪要用）")
            self.assertTrue(selectors.GROUPS[name].selectors)

    def test_open_conversation_js_is_callable(self):
        self.assertTrue(callable(selectors.open_conversation_js))
        self.assertTrue(callable(selectors.chat_header_js))


class OpenConversationTests(unittest.TestCase):
    def test_already_open_returns_fast(self):
        cdp = _FakeCDP(header="李森桦", editor_ready=True)
        with mock.patch.object(douyin, "_editor_ready", return_value=True):
            result = douyin.open_conversation(cdp, "李森桦")
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "already_open")
        self.assertNotIn("open_conversation", cdp.calls, "已在目标会话时不该再点列表")

    def test_not_found_reports_reason(self):
        cdp = _FakeCDP(open_result={"found": False, "reason": "no_match"})
        with mock.patch.object(douyin, "_editor_ready", return_value=True):
            result = douyin.open_conversation(cdp, "不存在的人", timeout=6.0)
        self.assertFalse(result["ok"])
        self.assertIn("找不到", result["reason"])

    def test_editor_not_ready_after_click_fails(self):
        cdp = _FakeCDP(open_result={"found": True, "clicked": True, "selector": "x"},
                       header="李森桦")
        with mock.patch.object(douyin, "_editor_ready", return_value=False):
            result = douyin.open_conversation(cdp, "李森桦", timeout=6.0)
        self.assertFalse(result["ok"])
        self.assertIn("输入框", result["reason"])

    def test_header_mismatch_aborts_to_avoid_wrong_recipient(self):
        cdp = _FakeCDP(open_result={"found": True, "clicked": True, "selector": "x"},
                       header="王五")
        with mock.patch.object(douyin, "_editor_ready", return_value=True):
            result = douyin.open_conversation(cdp, "李森桦", timeout=6.0)
        self.assertFalse(result["ok"])
        self.assertIn("王五", result["reason"])
        self.assertIn("发错人", result["reason"])

    def test_empty_nickname_rejected(self):
        result = douyin.open_conversation(_FakeCDP(), "   ")
        self.assertFalse(result["ok"])


class CancelOldSessionsTests(unittest.TestCase):
    """发起新登录前必须取消旧会话，否则会撞「浏览器并发已满」。"""

    def test_cancels_active_sessions_but_keeps_current(self):
        rows = [{"session_id": "old1"}, {"session_id": "old2"}, {"session_id": "keep"}]
        canceled: list[str] = []
        with mock.patch.object(login.db, "query_all", return_value=rows), \
                mock.patch.object(login, "cancel",
                                  side_effect=lambda sid: (canceled.append(sid) or True)):
            count = login.cancel_account_sessions(7, keep="keep")
        self.assertEqual(count, 2)
        self.assertEqual(canceled, ["old1", "old2"])

    def test_db_error_does_not_raise(self):
        with mock.patch.object(login.db, "query_all", side_effect=RuntimeError("boom")):
            self.assertEqual(login.cancel_account_sessions(7), 0)

    def test_start_qrcode_login_cancels_before_register(self):
        order: list[str] = []

        def fake_cancel(account_id, **kwargs):                 # noqa: ARG001
            order.append("cancel_old")
            return 0

        def fake_register(*args, **kwargs):                    # noqa: ARG001
            order.append("register")
            return mock.Mock()

        settings = mock.Mock()
        settings.demo_mode = False
        account = {"id": 3, "user_id": 1}
        with mock.patch.object(login, "cancel_account_sessions", side_effect=fake_cancel), \
                mock.patch.object(login, "_register", side_effect=fake_register), \
                mock.patch.object(login, "create_session", return_value={}), \
                mock.patch.object(login, "new_session_id", return_value="ls_x"), \
                mock.patch.object(login, "_ttl", return_value=180):
            login.start_qrcode_login(account, settings)
        self.assertEqual(order, ["cancel_old", "register"],
                         "必须在起新会话前取消旧会话，否则会报并发已满")


class FrontendPageSizeGuardTests(unittest.TestCase):
    """前端请求的 page_size 不能超过接口上限（200），否则接口 400、列表被清空。

    真实事故：任务编辑页用 page_size=500 拉好友 → /friends 直接 400 → catch 把
    friends 置空 → 界面显示「该账号还没有好友」，其实好友是存在的。
    """

    def test_no_page_size_over_api_limit(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        candidates = [root / "web" / "static" / "assets",
                      root.parent / "web" / "static" / "assets"]
        assets = next((p for p in candidates if p.is_dir()), None)
        self.assertIsNotNone(assets, "找不到前端 assets 目录")
        offenders = []
        for js in Path(assets).glob("*.js"):
            text = js.read_text(encoding="utf-8")
            for match in re.finditer(r"page_size:\s*(\d+)", text):
                if int(match.group(1)) > 200:
                    offenders.append(f"{js.name}: page_size={match.group(1)}")
        self.assertEqual(offenders, [],
                         f"这些请求会被接口拒绝（上限 200）：{offenders}")


class OpenConversationFallbackTests(unittest.TestCase):
    """会话列表是虚拟滚动：必须先滚动查找，再退到搜索框。"""

    def test_scrolls_until_found(self):
        rounds = {"count": 0}

        def fake_find(cdp, want):                       # noqa: ARG001
            rounds["count"] += 1
            return {} if rounds["count"] == 1 else {"found": True, "rect": {"cx": 1, "cy": 2}}

        cdp = mock.Mock()
        cdp.call.return_value = {"moved": True}
        with mock.patch.object(douyin, "_chat_header", return_value=""), \
                mock.patch.object(douyin, "_wait_conversation_rows", return_value=5), \
                mock.patch.object(douyin, "_editor_ready", return_value=True), \
                mock.patch.object(douyin, "_find_and_click_row", side_effect=fake_find), \
                mock.patch.object(douyin, "_open_by_search", return_value={"ok": False, "reason": ""}):
            result = douyin.open_conversation(cdp, "李森桦", timeout=10.0)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(rounds["count"], 2, "第一次没找到时必须滚动后再找")

    def test_falls_back_to_search(self):
        cdp = mock.Mock()
        cdp.call.return_value = {"moved": False}       # 滚不动了
        with mock.patch.object(douyin, "_chat_header", return_value=""), \
                mock.patch.object(douyin, "_wait_conversation_rows", return_value=5), \
                mock.patch.object(douyin, "_editor_ready", return_value=True), \
                mock.patch.object(douyin, "_find_and_click_row", return_value={}), \
                mock.patch.object(douyin, "_open_by_search",
                                  return_value={"ok": True, "via": "searched+clicked_conversation",
                                                "header": "李森桦", "nickname": "李森桦", "reason": ""}):
            result = douyin.open_conversation(cdp, "李森桦", timeout=10.0)
        self.assertTrue(result["ok"])
        self.assertIn("searched", result["via"])

    def test_failure_explains_where_it_stopped(self):
        cdp = mock.Mock()
        cdp.call.return_value = {"moved": False}
        with mock.patch.object(douyin, "_chat_header", return_value=""), \
                mock.patch.object(douyin, "_wait_conversation_rows", return_value=5), \
                mock.patch.object(douyin, "_editor_ready", return_value=True), \
                mock.patch.object(douyin, "_find_and_click_row", return_value={}), \
                mock.patch.object(douyin, "_open_by_search",
                                  return_value={"ok": False, "reason": "搜索「王五」也没有匹配的会话"}):
            result = douyin.open_conversation(cdp, "王五", timeout=10.0)
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["reason"], "", "失败必须给出可读原因")
        self.assertIn("搜索", result["reason"])


class SendPathTests(unittest.TestCase):
    """发送按钮 / 提交方式 / 列表等待 —— 都是实机踩过的坑。"""

    def test_send_button_never_matches_file_upload(self):
        # 实机：.messageMsgInputinputAction 是「图片/文件上传」按钮（内含 input[type=file]），
        # 误当成发送按钮只会弹出文件选择框，消息永远发不出去。
        sels = list(selectors.GROUPS["send_button"].selectors)
        for bad in sels:
            self.assertNotIn("inputAction", bad,
                             f"send_button 不能包含上传按钮选择器：{bad}")
            self.assertNotIn("file", bad.lower(),
                             f"send_button 不能包含文件上传相关选择器：{bad}")

    def test_committee_uses_enter_when_no_button(self):
        # 当前版本没有发送按钮 → 必须按 Enter 提交
        cdp = mock.Mock()
        cdp.select_all.return_value = None
        cdp.press_key = mock.Mock()
        with mock.patch.object(task_runner, "type_message", return_value=(True, "CDP insertText")), \
                mock.patch.object(task_runner.selectors, "click_group", return_value=None), \
                mock.patch.object(task_runner, "verify_sent", return_value=(True, "已发送")), \
                mock.patch.object(task_runner.humanize, "random_between", return_value=0.0):
            result = task_runner.send_message(cdp, "火花不能跳")
        self.assertTrue(result["ok"])
        self.assertIn("Enter", result["via"])
        cdp.press_key.assert_any_call("Enter")

    def test_open_conversation_reports_missing_list(self):
        cdp = mock.Mock()
        with mock.patch.object(douyin, "_chat_header", return_value=""), \
                mock.patch.object(douyin, "_wait_conversation_rows", return_value=5), \
                mock.patch.object(douyin, "_wait_conversation_rows", return_value=0):
            result = douyin.open_conversation(cdp, "李森桦", timeout=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("列表", result["reason"])

    def test_wait_rows_uses_both_selectors(self):
        cdp = mock.Mock()
        cdp.evaluate.return_value = 3
        rows = douyin._wait_conversation_rows(cdp, timeout=1.0)   # noqa: SLF001
        self.assertEqual(rows, 3)


class HarvestScrollTests(unittest.TestCase):
    """任务流程里 harvest 会把列表滚到底 → 找顶部好友前必须先滚回顶部。"""

    def test_open_conversation_scrolls_to_top_before_search(self):
        calls = []

        def fake_top(cdp):                        # noqa: ARG001
            calls.append("top")
            return {"ok": True}

        def fake_find(cdp, want):                 # noqa: ARG001
            calls.append("find")
            return {"found": True, "rect": {"cx": 1, "cy": 2}}

        cdp = mock.Mock()
        cdp.call.return_value = {"moved": False}
        with mock.patch.object(douyin, "_chat_header", return_value=""), \
                mock.patch.object(douyin, "_wait_conversation_rows", return_value=12), \
                mock.patch.object(douyin, "_scroll_conversations_top", side_effect=fake_top), \
                mock.patch.object(douyin, "_editor_ready", return_value=True), \
                mock.patch.object(douyin, "_find_and_click_row", side_effect=fake_find), \
                mock.patch.object(douyin, "_open_by_search", return_value={"ok": False, "reason": ""}):
            result = douyin.open_conversation(cdp, "七小只", timeout=10.0)
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], "top", "必须先滚回顶部再找（harvest 会把列表留在底部）")
        self.assertIn("find", calls)

    def test_harvest_returns_list_to_top(self):
        cdp = mock.Mock()
        cdp.call.return_value = {"moved": False}
        with mock.patch.object(douyin.selectors, "visible_texts", return_value=[]), \
                mock.patch.object(douyin, "_scroll_conversations_top") as top:
            douyin.harvest_conversations(cdp, scroll=True, max_scroll=1)
        top.assert_called_once()


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
