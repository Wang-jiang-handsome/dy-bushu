"""调度日额度 / 未匹配好友兜底 的回归测试（交付版，自包含）。

两个真实事故：

1. **「自动执行没有正常运行」**：用户白天手动点了几次「执行」（都成功），晚上到点时
   调度器查「今天该账号是否已成功」→ 命中手动那条 → 直接顺延到明天。
   现在 ``success_today`` **只认自动/重试触发**，手动自测不再吃掉当天自动额度
   （重复发送由按好友的当日幂等兜底）。
2. **「会话列表里没有找到该好友」**：虚拟滚动只抓前几屏，好友排在后面就匹配不到，
   旧逻辑直接 skipped。现在改成**继续尝试按昵称搜索打开会话**，能发就发。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.automation import task_runner
from app.worker import scheduler


class SuccessTodayTests(unittest.TestCase):
    def test_manual_runs_do_not_count(self):
        calls = {}

        def fake_query_one(sql, params=None):        # noqa: ARG001
            calls["sql"] = sql
            return None

        with mock.patch.object(scheduler.db, "query_one", side_effect=fake_query_one):
            scheduler.success_today(1, "2026-10-18")
        self.assertIn("trigger_type <> 'manual'", calls["sql"],
                      "手动执行必须排除，否则会吃掉当天的自动额度")

    def test_returns_row_when_auto_success(self):
        row = {"id": 9, "status": "success", "trigger_type": "scheduler"}
        with mock.patch.object(scheduler.db, "query_one", return_value=row):
            self.assertEqual(scheduler.success_today(1, "2026-10-18"), row)


class UnmatchedFallbackTests(unittest.TestCase):
    """未匹配到会话的好友也要继续尝试（搜索兜底），不能直接 skipped。"""

    def test_unmatched_friends_become_targets(self):
        outcome = task_runner.RunOutcome(run_id=1)
        targets = []
        unmatched = [{"id": 59, "nickname": "李森桦", "sec_uid": ""}]
        task = {"id": 2, "messages": ["火花不能断 🔥"], "friend_mode": "all"}
        account = {"id": 1, "user_id": 2}
        settings = mock.Mock(send_gap_seconds={"min": 0, "max": 0}, task_timeout_seconds=60)

        processed = []

        def fake_process(outcome, target, *args, **kwargs):   # noqa: ARG001
            processed.append(target.nickname)
            outcome.success += 1
            return "gap"

        with mock.patch.object(task_runner, "_process_target", side_effect=fake_process), \
                mock.patch.object(task_runner, "insert_item"), \
                mock.patch.object(task_runner, "_sleep_gap", return_value=0):
            task_runner._run_targets(        # noqa: SLF001
                outcome, targets, task, account, settings, "2026-10-18", False,
                1e9, __import__("random").Random(1), cdp=None, unmatched=unmatched)
        self.assertIn("李森桦", processed,
                      "未匹配到会话的好友也必须尝试打开（搜索兜底），不能直接跳过")
        self.assertEqual(outcome.skipped, 0)


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
