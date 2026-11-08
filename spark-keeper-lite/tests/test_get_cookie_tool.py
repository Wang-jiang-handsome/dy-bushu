"""``tools/get_cookie.py``（独立 Cookie 获取工具）的纯逻辑测试。

为什么值得测：这个工具是「扫码一直不出二维码」时的兜底路径，用户会把它当救命稻草。
它一旦算错 Cookie 头，粘进网页就是无效登录态，用户会以为是系统坏了。

这里只测**纯函数**（不启动浏览器、不连网），可在任何机器/CI 稳定运行：
* ``cookie_header`` 拼格式；
* ``pick_douyin_cookies`` 只保留抖音域名；
* ``is_login_cookie`` / ``has_login_cookie`` 认 sessionid、不认 ttwid；
* ``summarize`` 不回显 cookie 值（等价于密码）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


def _load_tool():
    """按「交付布局（root/tools）」或「开发布局（backend/../tools）」找到并加载工具模块。"""
    here = Path(__file__).resolve()
    root = here.parents[1]                       # 交付树=项目根；开发树=backend/
    candidates = [
        root / "tools" / "get_cookie.py",        # 交付布局
        root.parent / "tools" / "get_cookie.py",  # 开发布局（backend 的上一级）
    ]
    path = next((item for item in candidates if item.exists()), None)
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("get_cookie_tool", str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    if str(path.parents[1]) not in sys.path and (path.parents[1] / "app").exists():
        sys.path.insert(0, str(path.parents[1]))
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


@unittest.skipIf(TOOL is None, "找不到 tools/get_cookie.py")
class CookieHeaderTests(unittest.TestCase):
    def test_header_joins_name_value(self):
        header = TOOL.cookie_header([
            {"name": "sessionid", "value": "abc"},
            {"name": "ttwid", "value": "xyz"},
        ])
        self.assertEqual(header, "sessionid=abc; ttwid=xyz")

    def test_header_skips_empty_names(self):
        header = TOOL.cookie_header([{"name": "", "value": "x"},
                                     {"name": "sessionid", "value": "abc"}])
        self.assertEqual(header, "sessionid=abc")

    def test_header_keeps_empty_values(self):
        # 有些 cookie 的 value 就是空串，丢掉反而可能改变服务端判定
        self.assertEqual(TOOL.cookie_header([{"name": "a", "value": ""}]), "a=")


@unittest.skipIf(TOOL is None, "找不到 tools/get_cookie.py")
class DomainFilterTests(unittest.TestCase):
    def test_keeps_douyin_domains_only(self):
        cookies = [
            {"name": "sessionid", "value": "1", "domain": ".douyin.com"},
            {"name": "ttwid", "value": "2", "domain": "www.douyin.com"},
            {"name": "other", "value": "3", "domain": ".example.com"},
        ]
        picked = TOOL.pick_douyin_cookies(cookies)
        self.assertEqual([c["name"] for c in picked], ["sessionid", "ttwid"])

    def test_handles_missing_domain(self):
        self.assertEqual(TOOL.pick_douyin_cookies([{"name": "a", "value": "1"}]), [])


@unittest.skipIf(TOOL is None, "找不到 tools/get_cookie.py")
class LoginDetectionTests(unittest.TestCase):
    def test_sessionid_counts_as_login(self):
        self.assertTrue(TOOL.is_login_cookie({"name": "sessionid", "value": "abc"}))
        self.assertTrue(TOOL.has_login_cookie([
            {"name": "ttwid", "value": "x"},
            {"name": "sid_guard", "value": "y"},
        ]))

    def test_anonymous_cookies_are_not_login(self):
        self.assertFalse(TOOL.has_login_cookie([
            {"name": "ttwid", "value": "x"},
            {"name": "passport_csrf_token", "value": "z"},   # 打开弹层就下发，未登录也有
        ]))

    def test_empty_value_is_not_login(self):
        self.assertFalse(TOOL.is_login_cookie({"name": "sessionid", "value": "  "}))


@unittest.skipIf(TOOL is None, "找不到 tools/get_cookie.py")
class SummarizeTests(unittest.TestCase):
    def test_summary_never_leaks_values(self):
        cookies = [{"name": "sessionid", "value": "SUPER_SECRET_VALUE"},
                   {"name": "ttwid", "value": "also_secret"}]
        blob = json.dumps(TOOL.summarize(cookies), ensure_ascii=False)
        self.assertNotIn("SUPER_SECRET_VALUE", blob)
        self.assertNotIn("also_secret", blob)
        self.assertIn("sessionid", blob)


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
