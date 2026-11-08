"""部署产物回归测试：Dockerfile / compose / 部署脚本。

真实事故（国内网络）：

    ERROR [app] resolve image config for docker-image://docker.io/docker/dockerfile:1
    failed to do request ... dial tcp ...:443: i/o timeout

原因是 Dockerfile 第一行 `# syntax=docker/dockerfile:1` 要求 BuildKit 先去 docker.io
拉一个「Dockerfile 前端镜像」。本项目没用到任何 BuildKit 专属语法，所以那行纯属
给自己加一道外部依赖 —— 已删除，并用测试锁死，防止以后被「顺手加回来」。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

def _find_root() -> Path:
    """定位项目根目录。

    交付布局：<root>/tests/... 且 <root>/deploy/Dockerfile
    开发布局：spark-shared/backend/tests/...，deploy 在 spark-shared/deploy
    所以往上看两层，找第一个带 deploy/Dockerfile 的目录。
    """
    here = Path(__file__).resolve().parents[1]
    for cand in (here, here.parent, here.parent.parent):
        if (cand / "deploy" / "Dockerfile").is_file():
            return cand
    return here


ROOT = _find_root()
DEPLOY = ROOT / "deploy"


def read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


class DockerfileTests(unittest.TestCase):
    def test_no_buildkit_syntax_directive(self):
        for lineno, line in enumerate(read("Dockerfile").splitlines(), 1):
            self.assertFalse(
                line.strip().startswith("# syntax="),
                f"Dockerfile 第 {lineno} 行又加了 syntax 指令：国内网络会卡在拉前端镜像",
            )

    def test_starts_with_from(self):
        code = [l for l in read("Dockerfile").splitlines()
                if l.strip() and not l.strip().startswith("#")]
        self.assertTrue(code[0].startswith("FROM "), f"第一行有效指令应为 FROM，实际：{code[0]}")

    def test_has_apt_mirror_arg(self):
        text = read("Dockerfile")
        self.assertIn("ARG APT_MIRROR", text, "需要 APT_MIRROR 构建参数，否则国内 apt 会超时")
        self.assertIn("deb.debian.org", text, "应该替换 deb.debian.org，而不是写死某个源")

    def test_installs_chromium_and_cjk_fonts(self):
        text = read("Dockerfile")
        self.assertIn("chromium", text)
        self.assertIn("fonts-noto-cjk", text)


class ComposeTests(unittest.TestCase):
    def _compose_files(self) -> list[Path]:
        """不同版本带的 compose 文件不同：pro 只有 docker-compose.yml，
        lite 只有 docker-compose.lite.yml；两者根目录都有 docker-compose.yml。"""
        found = [p for p in (DEPLOY / "docker-compose.yml",
                             DEPLOY / "docker-compose.lite.yml",
                             ROOT / "docker-compose.yml") if p.is_file()]
        self.assertTrue(found, "一个 compose 文件都没找到")
        return found

    def test_every_build_has_apt_mirror_arg(self):
        for path in self._compose_files():
            text = path.read_text(encoding="utf-8")
            builds = len(re.findall(r"^\s+build:\s*$", text, re.M))
            args = len(re.findall(r"^\s+args:\s*$", text, re.M))
            mirrors = len(re.findall(r"^\s+APT_MIRROR:\s", text, re.M))
            self.assertGreater(builds, 0, f"{path.name} 里没有 build: 段")
            self.assertEqual(builds, args, f"{path.name}: {builds} 个 build 只有 {args} 个 args")
            self.assertEqual(builds, mirrors,
                             f"{path.name}: {builds} 个 build 只有 {mirrors} 个 APT_MIRROR")

    def test_compose_keeps_data_volume(self):
        for path in self._compose_files():
            text = path.read_text(encoding="utf-8")
            self.assertIn("/app/data", text, f"{path.name} 数据目录必须挂卷，否则容器重建丢库")


class DeployScriptTests(unittest.TestCase):
    def _check_shell(self, name: str) -> str:
        path = DEPLOY / name
        self.assertTrue(path.is_file(), f"缺少部署脚本：{path}")
        raw = path.read_bytes()
        self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", f"{name} 有 UTF-8 BOM，Linux 上会 bad interpreter")
        self.assertNotIn(b"\r\n", raw, f"{name} 必须是 LF 换行（Windows CRLF 会让 bash 报错）")
        return raw.decode("utf-8")

    def test_baota_install_script(self):
        text = self._check_shell("baota-install.sh")
        for needle in ("check_ssl", "check_sqlite", "--python", "--dry-run", "--uninstall"):
            self.assertIn(needle, text, f"baota-install.sh 缺少关键能力：{needle}")

    def test_docker_mirror_script(self):
        text = self._check_shell("setup-docker-mirror.sh")
        self.assertIn("registry-mirrors", text)
        self.assertIn("--restore", text)
        self.assertIn("data.setdefault", text)

    def test_systemd_unit_exists(self):
        text = read("spark-keeper.service")
        self.assertIn("[Service]", text)
        self.assertIn("ExecStart=", text)
        self.assertIn("Environment=HOME=", text, "Chromium 需要可写 HOME，否则浏览器起不来")


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main(verbosity=2)
