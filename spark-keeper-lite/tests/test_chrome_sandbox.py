"""Chromium 沙箱开关的回归测试（容器里启动失败的真实事故）。

现场日志：

    [zygote_host_impl_linux.cc:130] No usable sandbox!
    If this is a Debian system, please install the chromium-sandbox package ...
    （浏览器启动后立即退出（exit=1））

原因：容器里以**非 root** 用户运行时，Chromium 的 setuid sandbox 用不了
（没有 SUID helper；CentOS 7 宿主机内核 3.10 又没开 user namespace）。
以前的实现只判断 `geteuid() == 0`，容器里根本不会加 `--no-sandbox`。

注意：测试要在任意平台跑，所以统一把 `os.name` 打成 `posix`（Windows 上沙箱参数
本来就不加，另有专门用例断言这一点）。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.automation import cdp


def settings_with(mode: str):
    return SimpleNamespace(chrome_no_sandbox=mode, browser_headless=True,
                           browser_width=1366, browser_height=900)


def posix_env(*, container: bool = False, euid: int = 1000):
    """把「非 Windows + 容器标记 + euid」一起打桩。

    注意**不要**改 os.name：那样 pathlib 会尝试实例化 PosixPath 而抛
    UnsupportedOperation（Windows 上）。所以打桩的是 cdp._is_windows()。
    """
    return (
        mock.patch.object(cdp, "_is_windows", return_value=False),
        mock.patch.object(cdp, "in_container", return_value=container),
        mock.patch.object(os, "geteuid", return_value=euid, create=True),
    )


class NeedsNoSandboxTests(unittest.TestCase):
    def _call(self, mode: str, *, container: bool, euid: int) -> bool:
        patches = posix_env(container=container, euid=euid)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return cdp.needs_no_sandbox(settings_with(mode))

    def test_explicit_on(self):
        self.assertTrue(self._call("1", container=False, euid=1000))

    def test_explicit_off_beats_container(self):
        self.assertFalse(self._call("0", container=True, euid=0),
                         "显式关掉时不能再自作主张加 --no-sandbox")

    def test_root_needs_no_sandbox(self):
        self.assertTrue(self._call("auto", container=False, euid=0))

    def test_container_non_root_needs_no_sandbox(self):
        # 用户遇到的就是这一种：容器 + 非 root
        self.assertTrue(self._call("auto", container=True, euid=10001))

    def test_plain_host_keeps_sandbox(self):
        self.assertFalse(self._call("auto", container=False, euid=1000))

    def test_windows_never(self):
        with mock.patch.object(cdp, "_is_windows", return_value=True):
            self.assertFalse(cdp.needs_no_sandbox(settings_with("auto")))


class ChromeArgsTests(unittest.TestCase):
    def _args(self, mode: str, *, container: bool, euid: int):
        patches = posix_env(container=container, euid=euid)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return cdp.build_chrome_args(settings_with(mode), Path("/tmp/profile-xyz"))

    def test_flags_present_in_container(self):
        args = self._args("auto", container=True, euid=10001)
        self.assertIn("--no-sandbox", args)
        self.assertIn("--disable-setuid-sandbox", args)

    def test_flags_absent_on_plain_host(self):
        args = self._args("auto", container=False, euid=1000)
        self.assertNotIn("--no-sandbox", args)

    def test_force_on_adds_flags(self):
        args = self._args("1", container=False, euid=1000)
        self.assertIn("--no-sandbox", args)

    def test_no_dev_shm_flag_always(self):
        # /dev/shm 小是容器通病，这个参数无论如何都要带
        for container in (True, False):
            args = self._args("auto", container=container, euid=1000)
            self.assertIn("--disable-dev-shm-usage", args)


class DiagnoseHintsTests(unittest.TestCase):
    def test_sandbox_hint(self):
        hint = cdp._diagnose_chrome_failure(  # noqa: SLF001
            "No usable sandbox! Please install chromium-sandbox")
        self.assertIn("SK_CHROME_NO_SANDBOX=1", hint)
        self.assertIn("沙箱", hint)

    def test_zygote_hint(self):
        hint = cdp._diagnose_chrome_failure(  # noqa: SLF001
            "[1120:1120:0101/104552.873556:ERROR:content/browser/zygote_host/"
            "zygote_host_impl_linux.cc:130] x")
        self.assertIn("沙箱", hint)

    def test_shm_hint(self):
        hint = cdp._diagnose_chrome_failure("Failed to create shared memory region")  # noqa: SLF001
        self.assertIn("shm", hint.lower())

    def test_missing_library_hint(self):
        hint = cdp._diagnose_chrome_failure(  # noqa: SLF001
            "error while loading shared libraries: libnss3.so: cannot open shared object file")
        self.assertIn("libnss3", hint)

    def test_unknown_error_returns_empty(self):
        self.assertEqual(cdp._diagnose_chrome_failure("segfault at 0x0"), "")  # noqa: SLF001


class DeployDefaultsTests(unittest.TestCase):
    """镜像/编排必须默认关沙箱，否则「开箱即用」会直接失败。"""

    def _root(self) -> Path:
        here = Path(__file__).resolve().parents[1]
        for cand in (here, here.parent, here.parent.parent):
            if (cand / "deploy" / "Dockerfile").is_file():
                return cand
        return here

    def test_dockerfile_sets_no_sandbox(self):
        text = (self._root() / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("SK_CHROME_NO_SANDBOX", text, "镜像里应默认关闭 Chromium 沙箱")

    def test_compose_passes_no_sandbox(self):
        root = self._root()
        files = [p for p in (root / "deploy" / "docker-compose.yml",
                             root / "deploy" / "docker-compose.lite.yml",
                             root / "docker-compose.yml") if p.is_file()]
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIn("SK_CHROME_NO_SANDBOX", text, f"{path.name} 应显式传 SK_CHROME_NO_SANDBOX")

    def test_systemd_unit_sets_no_sandbox(self):
        text = (self._root() / "deploy" / "spark-keeper.service").read_text(encoding="utf-8")
        self.assertIn("SK_CHROME_NO_SANDBOX", text, "裸机（老内核）也要能启动 Chromium")


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
