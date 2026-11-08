"""接口测试包（队友 E）。

运行方式（任选，都在 ``spark-shared/`` 目录下执行）：

    python -m unittest discover -s tests -t . -v     # 全部
    python -m tests.test_smoke -v                    # 单个模块
    python tools/selftest.py                         # 一键汇总所有检查

约定
----
* 只用标准库（``unittest`` + ``urllib`` + ``sqlite3``），因为本机 pip 装不了包。
* 接口还没实现的用例会 ``skipTest`` 并打印「接口未就绪」，不会伪装成通过。
"""
