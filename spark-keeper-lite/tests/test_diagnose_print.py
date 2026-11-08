"""`main.py diagnose --print` 摘要输出的回归测试。

为什么需要它：用户在**远程服务器/容器**里跑诊断，取文件来回很痛苦
（真实场景：把 docker cp 出来的目录路径发过来，但文件还在服务器上，读不到）。
所以摘要必须能直接打印成「复制粘贴就能发」的文本，且包含排障所需的全部关键信息：

* 环境：Python / SQLite / 是否容器 / 是否有图形界面 / 浏览器路径 / headless / 沙箱开关
* 结果与错误原因、登录态、私信页可用性
* 页面取证：URL / 标题 / readyState 阻塞类型 / 会话条数 / 输入框数 / 正文片段
* 选择器命中组与样例文本
* 输出目录与文件清单
"""

from __future__ import annotations

import io
import unittest
from unittest import mock

import main as cli


class DiagnosePrintTests(unittest.TestCase):
    def _run(self, result: dict) -> str:
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            cli._print_diagnose_summary(result)
        return buffer.getvalue()

    def test_prints_environment_and_verdict(self):
        text = self._run({"ok": False, "message": "页面操作失败：超时", "out_dir": "",
                          "selectors": {}, "files": []})
        for token in ("环境", "python=", "sqlite=", "容器=", "浏览器=", "headless=",
                      "结果", "message=页面操作失败：超时"):
            self.assertIn(token, text, msg=text)

    def test_prints_selector_hits(self):
        text = self._run({
            "ok": True, "message": "诊断完成", "out_dir": "",
            "selectors": {
                "conversation_item": {"visible": 12, "total": 12, "sample": "李森桦 138 18:31"},
                "message_input": {"visible": 0, "total": 0, "sample": ""},
            },
            "files": ["report.json", "report.html", "screenshot.png"],
        })
        self.assertIn("命中组 1 个", text)
        self.assertIn("conversation_item", text)
        self.assertIn("report.html", text)
        # 没有命中的组不打印，保持摘要短
        self.assertNotIn("message_input", text)

    def test_prints_page_snapshot_from_report_json(self):
        import json
        import tempfile
        from pathlib import Path

        folder = Path(tempfile.mkdtemp(prefix="sk-diag-print-"))
        (folder / "report.json").write_text(json.dumps({
            "extra": {
                "logged_in": True, "login_reason": "session_cookie",
                "messages_ok": False, "messages_reason": "等待会话列表超时",
                "url": "https://www.douyin.com/chat?isPopup=1", "title": "",
                "error": "Runtime.evaluate 超时",
                "snapshot": {"wall": "blank", "conversations": 0, "editors": 0,
                             "body": "开启读屏标签 抖音聊天"},
            }
        }, ensure_ascii=False), encoding="utf-8")

        text = self._run({"ok": False, "message": "打不开私信页", "out_dir": str(folder),
                          "selectors": {}, "files": []})
        for token in ("logged_in=True", "会话列表超时", "阻塞=blank", "会话=0",
                      "读屏标签", "Runtime.evaluate 超时"):
            self.assertIn(token, text, msg=text)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main(verbosity=2)
