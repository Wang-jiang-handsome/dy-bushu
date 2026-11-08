"""跨域白名单回归测试（前端放 Netlify/静态托管、后端放服务器时用）。

设计取舍（写进测试免得将来被"优化"掉）：

* 生产环境默认**不开**跨域 —— 同源部署最简单也最安全；
* 只有 ``SK_CORS_ORIGINS`` 里精确列出的来源才放行（不是前缀匹配，防
  ``https://evil-app.example.com`` 这类"包含关系"绕过）；
* 鉴权走 ``Authorization`` 头，不用 Cookie，所以不需要 ``Allow-Credentials``。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.http_server import SparkHandler


class _FakeHandler:
    """只借用 ``_cors_headers``，不跑真实 socket。"""

    def __init__(self, origin: str, env: str = "prod", origins=()):
        self.headers = {"origin": origin} if origin else {}
        self.settings = SimpleNamespace(env=env, cors_origins=tuple(origins))

    _cors_headers = SparkHandler._cors_headers


class CorsHeaderTests(unittest.TestCase):
    def test_prod_same_origin_sends_nothing(self):
        handler = _FakeHandler("", env="prod", origins=("https://app.example.com",))
        self.assertNotIn("Access-Control-Allow-Origin", handler._cors_headers())

    def test_prod_allowlisted_origin_is_allowed(self):
        handler = _FakeHandler("https://app.example.com", env="prod",
                               origins=("https://app.example.com",))
        headers = handler._cors_headers()
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://app.example.com")
        self.assertEqual(headers.get("Vary"), "Origin")

    def test_prod_unknown_origin_is_blocked(self):
        handler = _FakeHandler("https://evil.example.com", env="prod",
                               origins=("https://app.example.com",))
        self.assertNotIn("Access-Control-Allow-Origin", handler._cors_headers())

    def test_prefix_lookalike_is_blocked(self):
        # 前缀相似不算：白名单必须精确匹配整个 origin
        handler = _FakeHandler("https://app.example.com.evil.com", env="prod",
                               origins=("https://app.example.com",))
        self.assertNotIn("Access-Control-Allow-Origin", handler._cors_headers())

    def test_trailing_slash_in_env_is_tolerated(self):
        handler = _FakeHandler("https://app.example.com", env="prod",
                               origins=("https://app.example.com",))
        self.assertEqual(handler._cors_headers().get("Access-Control-Allow-Origin"),
                         "https://app.example.com")

    def test_dev_allows_any_origin(self):
        handler = _FakeHandler("http://localhost:5173", env="dev", origins=())
        self.assertEqual(handler._cors_headers().get("Access-Control-Allow-Origin"),
                         "http://localhost:5173")

    def test_options_preflight_headers_present(self):
        handler = _FakeHandler("https://app.example.com", env="prod",
                               origins=("https://app.example.com",))
        headers = handler._cors_headers()
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])
        self.assertIn("OPTIONS", headers["Access-Control-Allow-Methods"])


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
