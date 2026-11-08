"""Chromium profile 单例锁 & 无头降级的回归测试。

真实事故（云服务器 / 容器）：

    The profile appears to be in use by another Chromium process (1592)
    on another computer (f8d034fc1c4d). Chromium has locked the profile ...
    Error: Can't open display:

1. `data/profiles/account_N` 若是**从别的机器复制的**（或容器 hostname 变了），
   里面残留的 `SingletonLock` 会让 Chromium 认为 profile 被别的电脑占用而拒绝启动。
   → `_clear_stale_profile_lock()` 在启动前判断并清理。
2. 有人把 `SK_BROWSER_HEADLESS=0` 配在无显示器的机器上 → `Can't open display`。
   → `effective_headless()` 会强制无头，哪怕配置要求可见窗口。
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.automation import cdp


class EffectiveHeadlessTests(unittest.TestCase):
    def test_config_headless_wins(self):
        with mock.patch.object(cdp, "display_available", return_value=True):
            self.assertTrue(cdp.effective_headless(SimpleNamespace(browser_headless=True)))

    def test_visible_allowed_when_display_present(self):
        with mock.patch.object(cdp, "display_available", return_value=True):
            self.assertFalse(cdp.effective_headless(SimpleNamespace(browser_headless=False)))

    def test_visible_forced_headless_without_display(self):
        with mock.patch.object(cdp, "display_available", return_value=False):
            self.assertTrue(
                cdp.effective_headless(SimpleNamespace(browser_headless=False)),
                "没有图形界面时必须强制无头，否则报 Can't open display",
            )


class StaleProfileLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sk-profile-lock-"))
        self.addCleanup(lambda: [p.unlink() for p in self.dir.iterdir()] and None
                        if False else None)

    def _write_lock(self, host: str, pid: int):
        (self.dir / "SingletonLock").write_text(f"{host}-{pid}", encoding="utf-8")
        (self.dir / "SingletonSocket").write_text("", encoding="utf-8")
        (self.dir / "SingletonCookie").write_text("123", encoding="utf-8")

    def test_clears_lock_from_another_host(self):
        self._write_lock("some-other-container", 1592)
        removed = cdp._clear_stale_profile_lock(self.dir)      # noqa: SLF001
        self.assertIn("SingletonLock", removed)
        self.assertFalse((self.dir / "SingletonLock").exists())
        self.assertFalse((self.dir / "SingletonSocket").exists())
        self.assertFalse((self.dir / "SingletonCookie").exists())

    def test_clears_lock_of_dead_pid_on_same_host(self):
        self._write_lock(socket.gethostname(), 999_999)         # 基本不可能存在的 PID
        removed = cdp._clear_stale_profile_lock(self.dir)      # noqa: SLF001
        if os.name == "nt":
            # Windows 上不能用 os.kill(pid, 0) 探测（会真杀进程），所以「同机 + 有 PID」
            # 一律保守当作活着；只有 hostname 不同才清。这里断言这个设计。
            self.assertEqual(removed, [], "Windows 上保守保留同机锁")
        else:
            self.assertIn("SingletonLock", removed)

    def test_keeps_lock_of_alive_pid_on_same_host(self):
        # Windows 上 _pid_alive 一律返回 True（os.kill 在 Windows 会真杀进程，不能用它探测）
        self._write_lock(socket.gethostname(), os.getpid())
        removed = cdp._clear_stale_profile_lock(self.dir)      # noqa: SLF001
        self.assertEqual(removed, [], "活着的进程持有锁时不能删，否则会并发破坏 profile")
        self.assertTrue((self.dir / "SingletonLock").exists())

    def test_noop_when_no_lock(self):
        self.assertEqual(cdp._clear_stale_profile_lock(self.dir), [])  # noqa: SLF001


class DiagnoseProfileLockTests(unittest.TestCase):
    def test_profile_in_use_hint(self):
        hint = cdp._diagnose_chrome_failure(  # noqa: SLF001
            "The profile appears to be in use by another Chromium process (1592)")
        self.assertIn("Singleton", hint)

    def test_cant_open_display_hint(self):
        hint = cdp._diagnose_chrome_failure("Error: Can't open display:")  # noqa: SLF001
        self.assertIn("图形界面", hint)


if __name__ == "__main__":                                      # pragma: no cover
    unittest.main(verbosity=2)
