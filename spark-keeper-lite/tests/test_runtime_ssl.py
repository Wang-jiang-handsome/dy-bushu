"""解释器能力缺失（`_ssl`）的回归测试。

真实事故：CentOS 7 上用宝塔装的 Python 编译时没带 OpenSSL → 没有 `_ssl` →
`app.api.auth` / `admin` / `user` 整块注册失败 → 登录页只显示「接口不存在」，
用户完全看不出原因（只有日志里有一行 `接口模块 app.api.auth 依赖缺失：_ssl`）。

修法分三层，测试各自锁死：
1. 启动时 `bootstrap.init_app` 打印醒目横幅，报告里带 `runtime_problems`；
2. 访问未注册接口时返回 **503 + code=python_missing_ssl**（而不是 404 not_found）；
3. `/api/v1/health` 暴露 `python_ssl` / `openssl`，方便监控与排障。

注意：`Response.body` 是**待序列化的 dict**（由 http_server 写出），不是 bytes。
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from app import bootstrap
from app.api import core
from app.http_server import SparkHandler


class RuntimeProblemsTests(unittest.TestCase):
    def test_empty_when_all_modules_available(self):
        self.assertEqual(bootstrap.runtime_problems(), [])

    def test_reports_missing_module(self):
        fake = (("definitely_not_a_module_xyz", "测试用缺失模块"),)
        with mock.patch.object(bootstrap, "RUNTIME_REQUIREMENTS", fake):
            problems = bootstrap.runtime_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("definitely_not_a_module_xyz", problems[0])
        self.assertIn("测试用缺失模块", problems[0])
        self.assertIn("ModuleNotFoundError", problems[0])

    def test_ssl_is_tracked(self):
        names = [name for name, _ in bootstrap.RUNTIME_REQUIREMENTS]
        self.assertIn("_ssl", names, "必须检查 _ssl：缺了它登录接口根本不会注册")


def missing_api_response(path: str):
    """直接调用处理器上的方法（不构造真实 socket）。"""
    handler = SimpleNamespace(headers={}, settings=SimpleNamespace(env="prod", cors_origins=()))
    return SparkHandler._missing_api_response(handler, path)      # noqa: SLF001


class MissingApiResponseTests(unittest.TestCase):
    def test_normal_404_when_ssl_available(self):
        resp = missing_api_response("/api/v1/auth/admin/login")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.body.get("code"), "not_found")

    def test_503_with_hint_when_ssl_missing(self):
        # sys.modules[name] = None 会让 `import name` 抛 ImportError
        with mock.patch.dict(sys.modules, {"_ssl": None}):
            resp = missing_api_response("/api/v1/auth/admin/login")
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.body.get("code"), "python_missing_ssl")
        message = str(resp.body.get("message") or "")
        self.assertIn("_ssl", message)
        # 必须给出可执行的修法，而不是只报错
        self.assertIn("uv python install", message)
        self.assertIn("openssl-devel", message)
        self.assertIn("Docker", message)
        self.assertEqual(resp.body.get("path"), "/api/v1/auth/admin/login")

    def test_other_api_paths_also_get_hint(self):
        with mock.patch.dict(sys.modules, {"_ssl": None}):
            resp = missing_api_response("/api/v1/user/profile")
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.body.get("code"), "python_missing_ssl")


class HealthExposesSslTests(unittest.TestCase):
    def _data(self):
        resp = core.health(SimpleNamespace(server=None))
        self.assertEqual(resp.status, 200)
        return (resp.body or {}).get("data") or {}

    def test_health_has_ssl_fields(self):
        data = self._data()
        self.assertIn("python_ssl", data)
        self.assertIn("openssl", data)

    def test_health_reports_true_on_this_interpreter(self):
        self.assertTrue(self._data().get("python_ssl"), "本机 Python 应该带 _ssl")


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
