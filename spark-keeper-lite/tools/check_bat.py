"""`.bat` / `.ps1` 编码与换行体检（Lead 维护）。

为什么单独做这个检查
--------------------
本会话里 `.bat` 的问题连续踩了三次（交付目录的 `start.bat` 是 UTF-8 + BOM + LF，
用户双击「黑窗口一闪就消失」，排查成本很高）。cmd.exe 对 `.bat` 有三个硬性要求，
任何一个不满足都会出现难以理解的失败：

* **纯 ASCII**：cmd 用 OEM 码页（中文 Windows 上是 GBK）读取 .bat，UTF-8 的中文
  会变成乱码，轻则 echo 乱码，重则被当成命令解析而中断。
* **CRLF**：只有 LF 时，行尾拼接、标签跳转在部分场景会出错。
* **无 BOM**：UTF-8 BOM 会让 cmd 把 `@echo off` 之前的字节当命令，报
  「'﻿@echo' 不是内部或外部命令」之类。
* `.ps1` 相反：**需要** UTF-8 BOM（否则中文注释在旧版 PowerShell 下会乱码/报错）。

用法:
    python tools/check_bat.py            # 扫描 spark-shared 与两份交付目录
退出码 0 = 全部合规。
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = [
    ROOT,
    ROOT.parent / "spark-keeper-pro",
    ROOT.parent / "spark-keeper-lite",
]
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "data", "vendor", "artifacts", ".venv"}


def check_bat(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        problems.append("有 UTF-8 BOM（cmd 会把 BOM 当命令，必须去掉）")
    non_ascii = [b for b in raw if b > 127]
    if non_ascii:
        problems.append(
            f"含 {len(non_ascii)} 个非 ASCII 字节（cmd 用 GBK 码页读取，中文会乱码/断行）"
        )
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    if bare_lf:
        problems.append(f"有 {bare_lf} 处裸 LF（.bat 必须全部用 CRLF）")
    if crlf == 0:
        problems.append("没有任何 CRLF 换行")
    if not raw.lstrip().lower().startswith(b"@echo off"):
        problems.append("首行不是 @echo off（会导致每条命令都被回显）")
    # 静默失败是「一闪就消失」的根源：所有 exit 之前都应有 pause
    text = raw.decode("ascii", "replace")
    exits = text.lower().count("exit /b")
    pauses = text.lower().count("pause")
    if exits and pauses == 0:
        problems.append("有 exit /b 但完全没有 pause（出错时窗口会一闪就消失）")
    return problems


def check_ps1(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_bytes()
    if raw[:3] != b"\xef\xbb\xbf":
        problems.append("缺少 UTF-8 BOM（PowerShell 读中文注释需要它）")
    return problems


def main() -> int:
    checked = 0
    problems: list[tuple[str, list[str]]] = []
    for base in TARGET_DIRS:
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                suffix = path.suffix.lower()
                if suffix == ".bat":
                    found = check_bat(path)
                elif suffix == ".ps1":
                    found = check_ps1(path)
                else:
                    continue
                checked += 1
                if found:
                    rel = str(path).replace(str(ROOT.parent) + os.sep, "")
                    problems.append((rel, found))
    print(f"检查 {checked} 个 .bat/.ps1 文件，有问题 {len(problems)} 个")
    for rel, items in problems:
        print(f"  ✗ {rel}")
        for item in items:
            print(f"      - {item}")
    if problems:
        print("\n修复：用 write 工具以「纯 ASCII + CRLF + 无 BOM」重写 .bat（中文提示放到 Python 里输出）。")
        return 1
    print("脚本体检通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
