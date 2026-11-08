"""可见窗口登录 / 每日不限次 / 日志参数 的回归测试（交付版，自包含）。

三个真事故：

1. **可见窗口登录一点就失败**：``_register(session_id, worker, account, settings, None, True)``
   多传了一个位置参数，工作线程一启动就 ``TypeError``，浏览器窗口根本没机会打开。
   用户看到的是「窗口里登录不弹浏览器」。
2. **任务执行报 ``log() got an unexpected keyword argument 'account_id'``**：
   ``sklog.info(..., account_id=...)`` —— ``log()`` 只接受
   ``(message, level, scope, user_id, source, context, persist)``。
3. **每日执行次数**：自托管默认不限次（``SK_UNLIMITED_TASK_RUNS=0`` 可恢复）。

这里都用「不启浏览器」的方式验证（mock / 纯函数 / 静态签名检查），CI 可跑。
"""

from __future__ import annotations

import ast
import inspect
import unittest
from unittest import mock

from app.automation import chrome, login
from app.common import logging as sklog
from app.common import quota as quotamod


class VisibleLoginWiringTests(unittest.TestCase):
    """可见窗口模式：线程参数必须对得上，否则启动就崩。"""

    def test_worker_accepts_five_positional_args(self):
        # _register 会以 (session_id, stop, account, settings, visible) 调用
        params = list(inspect.signature(login._real_qrcode_worker).parameters)  # noqa: SLF001
        self.assertGreaterEqual(len(params), 5, f"worker 参数不足：{params}")
        self.assertEqual(params[:5],
                         ["session_id", "stop", "account", "settings", "visible"])

    def test_start_qrcode_login_registers_visible_flag(self):
        """visible=True 时给工作线程传的必须是第 5 个参数 True（不多传 None）。"""
        captured: dict = {}

        def fake_register(session_id, target, *args):
            captured["target"] = target
            captured["args"] = args
            return mock.Mock()

        settings = mock.Mock()
        settings.demo_mode = False
        account = {"id": 1, "user_id": 1}
        with mock.patch.object(login, "_register", side_effect=fake_register), \
                mock.patch.object(login, "create_session", return_value={}), \
                mock.patch.object(login, "new_session_id", return_value="ls_test"), \
                mock.patch.object(login, "_ttl", return_value=180):
            login.start_qrcode_login(account, settings, visible=True)
        self.assertIs(captured["target"], login._real_qrcode_worker)  # noqa: SLF001
        self.assertEqual(captured["args"], (account, settings, True),
                         f"工作线程参数不对：{captured['args']}")

    def test_non_visible_registers_without_flag(self):
        captured: dict = {}

        def fake_register(session_id, target, *args):
            captured["args"] = args
            return mock.Mock()

        settings = mock.Mock()
        settings.demo_mode = False
        account = {"id": 1, "user_id": 1}
        with mock.patch.object(login, "_register", side_effect=fake_register), \
                mock.patch.object(login, "create_session", return_value={}), \
                mock.patch.object(login, "new_session_id", return_value="ls_test"), \
                mock.patch.object(login, "_ttl", return_value=180):
            login.start_qrcode_login(account, settings, visible=False)
        self.assertEqual(captured["args"], (account, settings))


class BrowserPriorityTests(unittest.TestCase):
    def test_windows_prefers_edge(self):
        # 用户要求：优先 Edge。候选表里 Edge 必须排在 Chrome 前面。
        win = [c for c in chrome.WINDOWS_CANDIDATES if c.lower().endswith((".exe",))]
        edge_idx = next(i for i, c in enumerate(win) if "msedge" in c.lower())
        chrome_idx = next(i for i, c in enumerate(win) if "chrome.exe" in c.lower())
        self.assertLess(edge_idx, chrome_idx, "Edge 应排在 Chrome 之前")

    def test_path_names_prefers_edge(self):
        names = [n.lower() for n in chrome.PATH_NAMES]
        self.assertLess(names.index("msedge"), names.index("chrome"))


class LoggingKwargTests(unittest.TestCase):
    """静态扫一遍：所有 sklog.* 调用不得传 log() 不认识的关键字。"""

    def test_no_invalid_log_kwargs(self):
        import pathlib
        allowed = set(inspect.signature(sklog.log).parameters)
        root = pathlib.Path(login.__file__).resolve().parents[2]   # app/ 的上一级
        bad: list[str] = []
        for path in root.rglob("app/**/*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                fn = node.func
                if not (isinstance(fn.value, ast.Name) and fn.value.id == "sklog"):
                    continue
                for kw in node.keywords:
                    if kw.arg and kw.arg not in allowed:
                        bad.append(f"{path.name}:{node.lineno} {kw.arg}")
        self.assertEqual(bad, [], f"这些 sklog 调用会 TypeError：{bad}")


class UnlimitedRunsTests(unittest.TestCase):
    def test_quota_reports_unlimited_flag(self):
        q = quotamod.Quota(user={"id": 1, "role": "user"}, quota=dict(quotamod.DEFAULT_QUOTA))
        with mock.patch.object(quotamod.Quota, "daily_task_unlimited",
                               new_callable=lambda: property(lambda self: True)):
            self.assertEqual(q.daily_task_limit, 0)
        summary_keys = set(quotamod.Quota.summary.__doc__ or "")
        self.assertTrue(summary_keys is not None)

    def test_summary_contains_unlimited_key(self):
        q = quotamod.Quota(user={"id": 1, "role": "user"}, quota=dict(quotamod.DEFAULT_QUOTA))
        with mock.patch.object(quotamod.Quota, "ensure"), \
                mock.patch.object(quotamod.Quota, "account_count", return_value=0), \
                mock.patch.object(quotamod.Quota, "today_run_count", return_value=0):
            summary = q.summary()
        self.assertIn("daily_task_unlimited", summary,
                      "前端靠这个字段把 x/y 显示成 x/∞")

    def test_ensure_can_run_today_skips_when_unlimited(self):
        q = quotamod.Quota(user={"id": 1, "role": "user"}, quota=dict(quotamod.DEFAULT_QUOTA))
        with mock.patch.object(quotamod.Quota, "ensure"), \
                mock.patch.object(quotamod.Quota, "daily_task_unlimited",
                                  new_callable=lambda: property(lambda self: True)), \
                mock.patch.object(quotamod.Quota, "today_run_count", return_value=99999):
            q.ensure_can_run_today()      # 不抛异常即通过


if __name__ == "__main__":                                          # pragma: no cover
    unittest.main(verbosity=2)
