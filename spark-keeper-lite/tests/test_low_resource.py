"""弱机器（1~2 核容器）性能降级项的回归测试。

真实事故（用户服务器）：登录态正常（`logged_in=True`），但抖音私信页只渲染出空壳
（正文只有「开启读屏标签 / 抖音聊天」），随后 `Runtime.evaluate` 连
`location.href` 都超时 —— 说明渲染进程被拖垮/假死，不是代码逻辑错。

修复方向是「降低浏览器在弱 CPU 上的开销 + 放宽超时」：

* `--disable-software-rasterizer` / `--disable-gpu-compositing`：软件光栅和 GPU 合成
  在弱 CPU 上是主要瓶颈（老日志里也有 "Automatic fallback to software WebGL"）；
* `SK_CHROME_LOW_RESOURCE=1`：再限制光栅线程数与渲染进程数；
* `SK_CDP_PAGE_TIMEOUT`：页面操作超时可调（默认 30s，容器里 45s）。
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from app.automation import cdp
from app.config import Settings


def settings(**kw):
    base = dict(browser_headless=True, chrome_no_sandbox="1", browser_width=1366,
                browser_height=900, chrome_low_resource=False)
    base.update(kw)
    return SimpleNamespace(**base)


class LowResourceFlagTests(unittest.TestCase):
    def _args(self, **kw):
        return " ".join(cdp.build_chrome_args(settings(**kw), "C:/tmp/profile"))

    def test_base_flags_always_disable_expensive_graphics(self):
        args = self._args()
        for flag in ("--disable-software-rasterizer", "--disable-gpu-compositing"):
            self.assertIn(flag, args, f"缺少 {flag}：弱 CPU 上软件光栅会拖死渲染进程")

    def test_low_resource_adds_limits(self):
        args = self._args(chrome_low_resource=True)
        for flag in ("--num-raster-threads=1", "--renderer-process-limit=2",
                     "--js-flags=--max-old-space-size=512"):
            self.assertIn(flag, args, f"低配模式缺少 {flag}")

    def test_low_resource_off_by_default(self):
        args = self._args()
        self.assertNotIn("--num-raster-threads=1", args)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        s = Settings()
        self.assertGreaterEqual(s.cdp_page_timeout, 20.0,
                                "页面操作超时太短会在弱机器上误判失败")
        self.assertFalse(s.chrome_low_resource, "默认不开启低配模式（按需打开）")

    def test_env_overrides(self):
        from app.config import load_settings

        with mock.patch.dict(os.environ, {"SK_CDP_PAGE_TIMEOUT": "60",
                                          "SK_CHROME_LOW_RESOURCE": "1"}, clear=False):
            cfg = load_settings(None)
        self.assertEqual(cfg.cdp_page_timeout, 60.0)
        self.assertTrue(cfg.chrome_low_resource)

    def test_bad_value_falls_back(self):
        from app.config import load_settings

        with mock.patch.dict(os.environ, {"SK_CDP_PAGE_TIMEOUT": "abc"}, clear=False):
            cfg = load_settings(None)
        self.assertEqual(cfg.cdp_page_timeout, 30.0)


class DeployDefaultsTests(unittest.TestCase):
    """容器镜像默认就应该按弱机器优化（用户机器就是 1~2 核容器）。"""

    def _root(self):
        from pathlib import Path
        here = Path(__file__).resolve().parents[1]
        for cand in (here, here.parent, here.parent.parent):
            if (cand / "deploy" / "Dockerfile").is_file():
                return cand
        return here

    def test_dockerfile_enables_low_resource(self):
        text = (self._root() / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("SK_CHROME_LOW_RESOURCE=1", text)
        self.assertIn("SK_CDP_PAGE_TIMEOUT", text)

    def test_compose_exposes_knobs(self):
        root = self._root()
        files = [p for p in (root / "deploy" / "docker-compose.yml",
                             root / "deploy" / "docker-compose.lite.yml",
                             root / "docker-compose.yml") if p.is_file()]
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIn("SK_CHROME_LOW_RESOURCE", text, path.name)
            self.assertIn("SK_CDP_PAGE_TIMEOUT", text, path.name)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main(verbosity=2)
