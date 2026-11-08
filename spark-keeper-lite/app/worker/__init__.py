"""Worker 层：并发闸门（pool）、队列与执行器（runner）、调度器（scheduler）。

⚠️ 这个 ``__init__`` **故意不 import 任何子模块**：``app.automation.session_store``
会 ``from ..worker import pool``，如果这里再 import runner/scheduler，而它们又
反向依赖 automation，就会形成循环导入（表现为「import app.worker.pool 失败」）。
需要用什么就显式 ``from app.worker.runner import ...``。
"""
