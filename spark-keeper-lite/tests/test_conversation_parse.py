"""真实抖音私信页（2026-10 实机）解析规则回归（交付版，自包含）。

实机抓到的三条关键事实，都在这里锁死：

1. 会话项 innerText 形如 ``李森桦\\n138\\n18:31\\n分享[视频]`` —— 昵称后面紧跟
   「火焰数 / 时间」，必须先截断再匹配，否则「李森桦 138」永远匹配不上好友
   「李森桦」（用户实测报 ``not_in_conversations``）。
2. 同一个会话会被**多个选择器**各抓一次，文本粒度不同（整行 / 标题行 / 昵称），
   不去重会抓到 81 条而实际只有 14 个会话 —— 批量发送时可能给同一个人发多次。
3. Slate 编辑器清空后残留 ``\\u200b``，不剔除会把「已清空=已发送」误判为失败。
"""

from __future__ import annotations

import unittest

from app.automation import douyin, task_runner


class ParseConversationTests(unittest.TestCase):
    def test_strips_fire_count_and_time(self):
        parsed = douyin.parse_conversations(["李森桦\n138\n18:31\n分享[视频]"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["nickname"], "李森桦")

    def test_strips_inline_tail(self):
        parsed = douyin.parse_conversations(["李森桦 138 18:31 分享[视频]"])
        self.assertEqual(parsed[0]["nickname"], "李森桦")

    def test_strips_chinese_time_words(self):
        for raw in ("卢建鑫 前天 在线", "姐姐 10/03 明天给我去买个", "七小只 443 刚刚 郭宇翔：[早点睡]"):
            nickname = douyin.parse_conversations([raw])[0]["nickname"]
            self.assertNotIn("前天", nickname)
            self.assertNotIn("10/03", nickname)
            self.assertNotIn("443", nickname)

    def test_dedupes_same_conversation_across_selectors(self):
        # 实机：同一会话被 3 个选择器各抓一次，粒度不同
        parsed = douyin.parse_conversations([
            "李洪亮的主人 366 21:13 李墨凡：[分享视频] 10",
            "李洪亮的主人\n366\n21:13",
            "李洪亮的主人",
        ])
        self.assertEqual([c["nickname"] for c in parsed], ["李洪亮的主人"],
                         "同一会话必须只保留一条，否则可能重复发送")

    def test_keeps_distinct_conversations(self):
        parsed = douyin.parse_conversations(["李森桦 138 18:31", "王小明 7 19:00"])
        self.assertEqual([c["nickname"] for c in parsed], ["李森桦", "王小明"])

    def test_keeps_emoji_in_nickname(self):
        parsed = douyin.parse_conversations(["睡个好觉💤 10/01 对方回复"])
        self.assertEqual(parsed[0]["nickname"], "睡个好觉💤")

    def test_drops_overlong_container_text(self):
        # 选择器命中了整块容器（文本很长）→ 宁可不匹配，也不发错人
        long_text = "很长的容器文本" * 10
        self.assertEqual(douyin.parse_conversations([long_text]), [])

    def test_empty_input(self):
        self.assertEqual(douyin.parse_conversations([]), [])
        self.assertEqual(douyin.parse_conversations(["\n\n  \n"]), [])


class EditorTextTests(unittest.TestCase):
    def test_strips_zero_width_characters(self):
        class FakeCDP:
            def __init__(self, value):
                self.value = value

            def call(self, *args, **kwargs):        # noqa: ARG002
                return {"value": self.value}

        # Slate 清空后只剩零宽空格 → 必须判为「空」
        self.assertEqual(task_runner.editor_text(FakeCDP("\u200b"), "message_input"), "")
        self.assertEqual(task_runner.editor_text(FakeCDP("\u200b \u00a0\ufeff"), "message_input"), "")
        self.assertEqual(task_runner.editor_text(FakeCDP("火花🔥\u200b"), "message_input"), "火花🔥")


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
