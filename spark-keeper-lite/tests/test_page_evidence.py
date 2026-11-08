"""页面取证 / 阻塞判定 / 探测超时 的回归测试。

真实事故（云服务器）：

    page_unavailable: 打不开私信页：等待会话列表超时（当前页面：
                      https://www.douyin.com/chat?isPopup=1；已补一次导航/重载）
    页面操作失败: Runtime.evaluate: 等待 CDP 响应超时（2s）

两种原因都归结为「错误信息里没有页面证据」+「探测器超时太短」：

1. 只报「超时」，用户不知道页面是空白、卡在滑块验证、还是登录失效 →
   `page_snapshot()` 收集 URL/标题/readyState/正文/会话条数，`detect_wall()`
   判定 captcha/login/network/blank，失败时连截图一起落到 data/diagnostics/。
2. 探测元素的超时曾写死 2 秒，CPU 被限流的容器里一条 JS 都可能超时 →
   统一提到 `PROBE_TIMEOUT`（8 秒，仍远小于整体等待预算）。
"""

from __future__ import annotations

import io
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.automation import douyin


class DetectWallTests(unittest.TestCase):
    def test_captcha(self):
        snap = {"title": "验证", "body": "请完成安全验证，拖动滑块"}
        self.assertEqual(douyin.detect_wall(snap), "captcha")

    def test_login(self):
        snap = {"title": "抖音", "body": "扫码登录 立即登录"}
        self.assertEqual(douyin.detect_wall(snap), "login")

    def test_network(self):
        snap = {"title": "抖音", "body": "网络异常，请重试"}
        self.assertEqual(douyin.detect_wall(snap), "network")

    def test_blank(self):
        self.assertEqual(douyin.detect_wall({"title": "", "body": "   "}), "blank")

    def test_normal_page_has_no_wall(self):
        snap = {"title": "私信", "body": "李森桦 发送消息"}
        self.assertEqual(douyin.detect_wall(snap), "")


class SummarizeTests(unittest.TestCase):
    def test_summary_contains_key_fields(self):
        text = douyin.summarize_snapshot({
            "title": "私信", "ready": "complete", "conversations": 0,
            "editors": 0, "wall": "captcha", "body": "请完成安全验证",
        })
        for token in ("标题", "ready=complete", "会话条数=0", "阻塞=captcha", "安全验证"):
            self.assertIn(token, text, msg=text)

    def test_summary_truncates_long_body(self):
        text = douyin.summarize_snapshot({"body": "很长的正文" * 100})
        self.assertLess(len(text), 400)


class PageSnapshotTests(unittest.TestCase):
    def test_collects_fields_from_fake_cdp(self):
        class FakeCDP:
            def evaluate(self, expr, timeout=None):        # noqa: ARG002
                if "location.href" in expr:
                    return "https://www.douyin.com/chat?isPopup=1"
                if "document.title" in expr:
                    return "私信"
                if "readyState" in expr:
                    return "complete"
                if "conversation-item" in expr:
                    return 0
                if "slate-editor" in expr:
                    return 0
                if "innerText" in expr:
                    return "请完成安全验证"
                return None

        snap = douyin.page_snapshot(FakeCDP())
        self.assertIn("douyin.com/chat", snap["url"])
        self.assertEqual(snap["title"], "私信")
        self.assertEqual(snap["ready"], "complete")
        self.assertEqual(snap["conversations"], 0)
        self.assertEqual(snap["wall"], "captcha")

    def test_survives_broken_cdp(self):
        class BrokenCDP:
            def evaluate(self, expr, timeout=None):        # noqa: ARG002
                raise RuntimeError("CDP 已断开")

        snap = douyin.page_snapshot(BrokenCDP())
        self.assertEqual(snap["url"], "")
        self.assertEqual(snap["wall"], "blank")       # 拿不到正文 → 空白页


class ProbeTimeoutTests(unittest.TestCase):
    def test_probe_timeout_is_generous(self):
        self.assertGreaterEqual(douyin.PROBE_TIMEOUT, 5.0,
                                "容器里 2s 太短：真实事故就是 Runtime.evaluate 超时（2s）")

    def test_no_hardcoded_two_second_probe_left(self):
        """自动化层不要再出现 timeout=2.0 的元素探测（会误报超时）。"""
        root = Path(douyin.__file__).resolve().parent
        offenders = []
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"timeout=2\.0\b", text):
                # 取上下文（跨行调用很常见），排除「不涉及页面渲染」的用法
                ctx_start = max(0, m.start() - 300)
                context = text[ctx_start:m.end() + 80]
                if any(k in context for k in ("process.wait", "detachFromTarget",
                                              "_probe_port", "Target.closeTarget")):
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                line = text[line_start:text.find("\n", m.start())]
                offenders.append(f"{path.name}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "元素探测超时 2s 太短（容器里会误报），请用 PROBE_TIMEOUT：" + str(offenders))

    def test_evidence_saved_for_failure(self):
        """open_chat 失败分支必须带页面取证信息（截图 + 摘要）。"""
        source = io.open(douyin.__file__, encoding="utf-8").read()
        self.assertIn("save_snapshot_evidence", source)
        self.assertIn("页面取证", source)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main(verbosity=2)
