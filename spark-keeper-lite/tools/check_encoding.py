"""交付目录编码体检：找出乱码（UTF-8 被 GBK 解读）与 BOM。

为什么需要：PowerShell 的 `Set-Content -Encoding utf8` 会把中文写成乱码并加 BOM，
本会话里已踩过 3 次（.gitignore、VERSION、以及一次临时脚本）。
交付物里带着中文文档，一旦乱码就是明显的质量问题，所以做成可重复的检查。
"""

from __future__ import annotations

import io
import os
import re
import sys

ROOTS = [
    r"D:\DeppSeek WorkRegion\spark-keeper-pro",
    r"D:\DeppSeek WorkRegion\spark-keeper-lite",
    r"D:\DeppSeek WorkRegion\spark-shared",
]
# GBK 误读 UTF-8 后出现的字符集（正常中文里几乎不会同时出现这些字）
SIGNATURE = re.compile(r"[浜氬锛鐨鏄涓鍜璁鍔绔鏂鐢鎶澶鐫娴浠鑳鏇鏈瀛鎴愬摝鐩鏃璺鍏撳娴嬭瘯鍛婄煡]")
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "data", "vendor"}
# 本文件自己含有乱码"特征字"（它就是靠这些字做检测的），必须排除，否则自指误报
SELF_NAMES = {"check_encoding.py"}
EXTS = {
    ".md", ".py", ".js", ".html", ".css", ".json", ".yml", ".yaml", ".sh",
    ".example", ".txt", ".sql", ".conf", ".mjs", ".bat", ".gitignore", ".dockerignore",
}


def main() -> int:
    problems: list[tuple[str, str]] = []
    scanned = 0
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name in SELF_NAMES:
                    continue
                ext = os.path.splitext(name)[1]
                if ext not in EXTS and name not in (".gitignore", ".dockerignore"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    raw = io.open(path, "rb").read()
                except OSError:
                    continue
                scanned += 1
                rel = path.replace("D:\\DeppSeek WorkRegion\\", "")
                if raw[:3] == b"\xef\xbb\xbf" and ext != ".bat":
                    problems.append((rel, "有 UTF-8 BOM（非 .bat 文件不需要）"))
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    problems.append((rel, "不是合法 UTF-8"))
                    continue
                hits = SIGNATURE.findall(text)
                if hits:
                    problems.append((rel, f"乱码特征 {len(hits)} 处：{''.join(hits[:5])}"))
    print(f"扫描 {scanned} 个文件，可疑 {len(problems)} 个")
    for rel, why in sorted(problems):
        print(f"  ✗ {rel} -> {why}")
    if problems:
        print("\n修复建议：这些文件是 PowerShell 的 Set-Content -Encoding utf8 写坏的。")
        print("请用 write 工具或 Python 以 UTF-8 无 BOM 重写（io.open(p,'w',encoding='utf-8',newline='\\n')）。")
        return 1
    print("编码体检通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
