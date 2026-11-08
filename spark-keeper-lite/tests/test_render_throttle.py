"""「页面壳出来了但列表不渲染 / JS 假死」的回归测试。

真实事故（云服务器，无头浏览器）：

    打不开私信页：等待会话列表超时（当前页面：https://www.douyin.com/chat?isPopup=1）
      页面取证：标题=«» ready=complete 会话条数=0 输入框=0 阻塞=无
                正文=«开启读屏标签 读屏标签已关闭 抖音聊天»
    页面操作失败：Runtime.evaluate: 等待 CDP 响应超时（8s）

页面只渲染出空壳、随后 JS 完全不响应 —— 这是「页面被判为后台/被遮挡，渲染与定时器
被节流」的典型表现。修复分三处，逐一用测试锁死：

1. Chromium 启动参数关掉遮挡检测（``CalculateNativeWinOcclusion``）；
2. 连上页面后主动 ``Emulation.setFocusEmulationEnabled`` + ``Page.setWebLifecycleState``；
3. ``open_chat`` 遇到 JS 超时会重载一次并重新前台化，失败原因区分「渲染假死」。
"""

from __future__ import annotations

import io
import re
import unittest
from types import SimpleNamespace

from app.automation import cdp, douyin


class LaunchFlagTests(unittest.TestCase):
    def _args(self):
        return cdp.build_chrome_args(
            SimpleNamespace(browser_headless=True, chrome_no_sandbox="1", browser_width=1366,
                            browser_height=900),
            "C:/tmp/profile-xyz",
        )

    def test_disables_occlusion_calculation(self):
        args = self._args()
        joined = " ".join(args)
        self.assertIn("CalculateNativeWinOcclusion", joined,
                      "不关掉遮挡检测，无头窗口会被判为不可见 → 不渲染")

    def test_disables_background_throttling(self):
        joined = " ".join(self._args())
        for flag in ("--disable-background-timer-throttling",
                     "--disable-renderer-backgrounding",
                     "--disable-backgrounding-occluded-windows"):
            self.assertIn(flag, joined, f"缺少 {flag}")


class ForegroundEmulationTests(unittest.TestCase):
    def test_sends_focus_and_lifecycle(self):
        session = SimpleNamespace()
        calls = []

        def fake_send(method, params=None, timeout=None):        # noqa: ARG001
            calls.append((method, params))

        session.send = fake_send
        cdp.CDPSession.enable_foreground(session)
        methods = [m for m, _ in calls]
        self.assertIn("Emulation.setFocusEmulationEnabled", methods)
        self.assertIn("Page.setWebLifecycleState", methods)
        lifecycle = [p for m, p in calls if m == "Page.setWebLifecycleState"][0]
        self.assertEqual(lifecycle.get("state"), "active")

    def test_survives_errors(self):
        session = SimpleNamespace()

        def boom(*args, **kwargs):                              # noqa: ARG001
            raise cdp.CDPError("老版本没有这个方法")

        session.send = boom
        cdp.CDPSession.enable_foreground(session)                # 不应抛异常

    def test_open_chat_applies_foreground(self):
        source = io.open(douyin.__file__, encoding="utf-8").read()
        self.assertIn("_foreground(cdp)", source,
                      "导航后必须重新前台化（导航可能重置生命周期状态）")


class StallRecoveryTests(unittest.TestCase):
    def test_open_chat_handles_timeout_and_reports_stall(self):
        source = io.open(douyin.__file__, encoding="utf-8").read()
        self.assertIn("js_stalled", source, "应记录「渲染假死」状态")
        self.assertIn("渲染进程无响应", source, "失败原因里要写明是 JS 无响应")
        self.assertIn("SK_MAX_CONCURRENT_BROWSERS", source,
                      "应给出可执行的建议（限流容器降并发）")

    def test_open_chat_reloads_once_on_timeout(self):
        source = io.open(douyin.__file__, encoding="utf-8").read()
        # 超时分支里应有 navigate(重载) 与 continue
        idx = source.find("js_stalled = True")
        self.assertGreater(idx, 0)
        window = source[idx:idx + 700]
        self.assertIn("navigate", window)
        self.assertIn("continue", window)


class DiagnoseWritesOnFailureTests(unittest.TestCase):
    def test_failure_helper_exists(self):
        from app.automation import diagnostics

        self.assertTrue(hasattr(diagnostics, "_write_failure"),
                        "诊断失败也必须落盘（用户遇到过空目录）")

    def test_all_failure_branches_write_files(self):
        from app.automation import diagnostics

        source = io.open(diagnostics.__file__, encoding="utf-8").read()
        # 注意：文件里有**两个** CDPError 分支 —— 内层那个是取证后 raise，
        # 外层才是返回失败结果。这里按各自的特征串定位。
        markers = (
            ("except session_store.BrowserBusy", "浏览器并发已满"),
            ("except session_store.BrowserUnavailable", "浏览器不可用"),
            ('message = f"页面操作失败', "页面操作失败"),
        )
        for anchor, needle in markers:
            idx = source.find(anchor)
            self.assertGreater(idx, 0, anchor)
            window = source[idx:idx + 700]
            self.assertIn("_write_failure", window,
                          f"{needle} 分支也要写报告，否则目录是空的")

    def test_writes_report_json(self):
        import tempfile
        from pathlib import Path

        from app.automation import diagnostics

        folder = Path(tempfile.mkdtemp(prefix="sk-diag-failure-"))
        files = diagnostics._write_failure(folder, 1, {"nickname": "测试"}, "浏览器不可用：x")
        self.assertTrue(files or (folder / "report.json").exists(),
                        f"应写出报告文件，实际：{files}")


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
