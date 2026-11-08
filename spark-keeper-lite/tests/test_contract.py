"""契约一致性测试：文档 ⇄ 运行时路由表。

真正的比对逻辑在 ``tools/check_contract.py``（Lead 验收也要跑它），
这里只是把它包成 unittest，让 ``python -m unittest discover`` 一起跑。

红灯时的含义
------------
* ``文档有代码无``：前端会踩 404 —— 必须实现。
* ``代码有文档无``：文档失真 —— 必须补文档。
* ``跨文件重复注册``：router 静默覆盖，响应体形状随 import 顺序漂移 —— 必须删一个。

运行：
    cd spark-shared
    python -m unittest tests.test_contract -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_contract as cc  # noqa: E402


def _detect_profile() -> str:
    """交付包是 pro 还是 lite：有支付模块就是 pro。

    lite 包按设计没有 ``app/payment``，契约里那两条 ``/pay/*`` 就该「不存在」——
    所以 lite 树必须用 lite 档位跑，否则会把设计差异误报成缺陷。
    """
    backend = cc.BACKEND_DIR
    has_payment = (backend / "app" / "payment").is_dir() or (cc.PROJECT_ROOT / "pro" / "app" / "payment").is_dir()
    return "pro" if has_payment else "lite"


class TestContractDocVsRuntime(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = _detect_profile()
        try:
            cls.report = cc.build_report(profile=cls.profile)
        except cc.RuntimeUnavailable as exc:
            raise unittest.SkipTest(f"运行时代码不可用，契约检查无从谈起：{exc}") from exc

    # ---- 解析器自身的健康度（防止「检查通过」其实是因为没解析到东西）----
    def test_00_doc_parsed(self) -> None:
        self.assertIn(self.profile, ("pro", "lite"))
        self.assertGreater(self.report["doc_total"], 90,
                           msg=f"只解析出 {self.report['doc_total']} 个接口，文档解析器可能坏了")
        self.assertGreater(self.report["runtime_total"], 10,
                           msg=f"只读到 {self.report['runtime_total']} 条运行时路由，API 模块可能没加载")

    def test_01_documented_paths_exist_in_code(self) -> None:
        missing = self.report["missing_in_code"]
        if not missing:
            return
        detail = "\n".join(f"  - {'/'.join(m['methods']):<22} {m['path']}   ({m['doc']})" for m in missing)
        self.fail(f"文档有代码无：{len(missing)} 处（前端会 404）\n{detail}")

    def test_02_code_paths_are_documented(self) -> None:
        extra = self.report["undocumented_in_code"]
        if not extra:
            return
        detail = "\n".join(f"  - {'/'.join(e['methods']):<22} {e['path']}" for e in extra)
        self.fail(f"代码有文档无：{len(extra)} 处（请补 docs/API_CONTRACT.md）\n{detail}")

    def test_03_no_duplicate_registration(self) -> None:
        duplicates = self.report["duplicate_routes"]
        if not duplicates:
            return
        lines: list[str] = []
        for item in duplicates:
            lines.append(f"  - {item['method']} {item['path']}")
            lines.extend(f"        注册于 {place}" for place in item["places"])
        self.fail("跨文件重复注册同一接口（router 会静默覆盖，只保留最后 import 的）：\n"
                  + "\n".join(lines))

    def test_04_method_differences_are_reported(self) -> None:
        """方法差异默认只警告：打印出来但不判失败（多方法简写行容易误伤）。"""
        gaps = self.report["method_missing_in_code"]
        extra = self.report["method_extra_in_code"]
        if gaps or extra:
            print(f"\n    [警告] 方法差异：文档声明但未实现 {len(gaps)} 处，代码多出 {len(extra)} 处")
            for item in gaps:
                print(f"      - {item['path']} 缺 {'/'.join(item['missing'])}"
                      f"（代码有 {'/'.join(item['code_has'])}）")
            for item in extra:
                print(f"      - {item['path']} 多 {'/'.join(item['extra'])}"
                      f"（文档只写了 {'/'.join(item['doc_has'])}）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
