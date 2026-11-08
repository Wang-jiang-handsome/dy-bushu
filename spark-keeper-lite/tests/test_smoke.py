"""端到端冒烟测试：起真实 HTTP 服务，用 urllib 跑完整业务链路。

链路（能跑多少跑多少，没实现的接口会显示为 skip 而不是假通过）：
    健康检查 → 安装体检 → 一键安装 → 管理员登录
    → 注册/登录 → 用户信息 → 抖音账号 → 定时任务 → 执行记录
    → 越权隔离 → 卡密生成/兑换（含重复兑换 409）→ 下单 → 管理端统计

运行：
    cd spark-shared
    python -m unittest tests.test_smoke -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_harness import (  # noqa: E402
    ADMIN_PASSWORD,
    USER_PASSWORD,
    ApiResponse,
    ServerHarness,
    json_dump,
)

# 契约 §0 的错误码白名单：任何接口返回的 code 都必须在这里面
ERROR_CODES = {
    "bad_request", "unauthorized", "payment_required", "forbidden", "not_found",
    "method_not_allowed", "conflict", "payload_too_large", "too_many_requests", "server_error",
    "no_active_package", "account_limit", "task_daily_limit", "card_invalid", "card_used",
    "task_running", "need_verify", "login_failed", "locked", "pay_disabled", "pay_channel_error",
}


class SmokeBase(unittest.TestCase):
    """共享一个服务实例（模块级全局路由表 + 单例 settings，别开两个）。"""

    server: ServerHarness

    @classmethod
    def setUpClass(cls) -> None:
        # 每个测试类一个独立服务 + 独立数据目录（同名目录会互相污染）
        cls.server = ServerHarness(cls.__name__.lower()).start()
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    # ---- 工具 ----
    def require(self, method: str, path: str, why: str = "") -> None:
        """接口没注册就跳过（并说清楚在等谁），而不是让整条链红掉。"""
        if not self.server.has_route(method, path):
            self.skipTest(f"接口未就绪：{method} {path} {why}".strip())

    def check_envelope(self, resp: ApiResponse, expect_status: int = 200) -> None:
        self.assertEqual(resp.status, expect_status, msg=json_dump(resp.body))
        self.assertIsInstance(resp.body, dict, msg=json_dump(resp.body))
        self.assertIn("code", resp.body, msg=json_dump(resp.body))
        if resp.status < 400:
            self.assertEqual(resp.body["code"], 0, msg=json_dump(resp.body))
            self.assertIn("data", resp.body, msg=json_dump(resp.body))
        else:
            self.assertIn(resp.body["code"], ERROR_CODES,
                          msg=f"错误码不在契约白名单内：{json_dump(resp.body)}")

    def login_admin(self) -> str:
        """用夹具直接建管理员并签发令牌（不依赖登录接口是否写完）。"""
        admin = self.server.create_admin(password=ADMIN_PASSWORD)
        return self.server.token_for(admin)

    def login_user(self, username: str = "journey") -> tuple[str, dict]:
        user = self.server.create_user(username=username, password=USER_PASSWORD)
        return self.server.token_for(user), user


class TestMinimalChain(SmokeBase):
    """最小链路：起服务 → 健康检查 → 安装体检。不需要队友 A/B 的任何代码。"""

    def test_01_health(self) -> None:
        resp = self.server.get("/api/v1/health")
        self.check_envelope(resp)
        data = resp.data or {}
        # 契约 §1：存活探针返回版本 / 运行时长 / 请求数。
        # 字段名不做过度绑定（core.py 与 install.py 的实现细节不同），只要求语义齐备。
        self.assertTrue(data.get("version"), msg=json_dump(data))
        self.assertIsInstance(data.get("uptime_seconds"), int, msg=json_dump(data))
        db_flag = data.get("db_ok", data.get("database", data.get("ok")))
        self.assertTrue(db_flag, msg="health 必须能证明数据库可用：" + json_dump(data))
        print(f"\n    health -> {json_dump(data)}")

    def test_02_routes_meta(self) -> None:
        resp = self.server.get("/api/v1/_routes")
        self.check_envelope(resp)
        routes = (resp.data or {}).get("routes") or []
        self.assertGreater(len(routes), 0)
        paths = {r["path"] for r in routes}
        self.assertIn("/api/v1/health", paths)
        for entry in routes:      # 路由清单每项必须能描述自己的鉴权级别
            self.assertIn(entry.get("auth"), ("public", "user", "admin"), msg=json_dump(entry))
            self.assertIn(entry.get("method"), ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"))
        print(f"\n    已注册接口 {len(routes)} 条")

    def test_03_unknown_route_404(self) -> None:
        resp = self.server.get("/api/v1/definitely-not-a-route")
        self.check_envelope(resp, 404)
        self.assertEqual(resp.code, "not_found", msg=json_dump(resp.body))

    def test_04_wrong_method_405(self) -> None:
        resp = self.server.post("/api/v1/health", {})
        self.check_envelope(resp, 405)
        self.assertEqual(resp.code, "method_not_allowed", msg=json_dump(resp.body))

    def test_05_install_check(self) -> None:
        self.require("GET", "/api/v1/install/check", "（等队友 B）")
        resp = self.server.get("/api/v1/install/check")
        self.check_envelope(resp)
        data = resp.data or {}
        for key in ("installed", "schema_version", "database", "environment", "site", "version"):
            self.assertIn(key, data, msg=json_dump(data))
        self.assertTrue((data.get("database") or {}).get("ok"), msg=json_dump(data))
        self.assertGreater(int((data.get("database") or {}).get("tables") or 0), 20, msg=json_dump(data))
        self.assertIn("python_ok", data.get("environment") or {}, msg=json_dump(data))
        self.assertFalse(data["installed"], msg="空数据目录不该已安装：" + json_dump(data))
        print(f"\n    install/check -> {json_dump(data)}")

    def test_06_install_latest_migration(self) -> None:
        self.require("GET", "/api/v1/install/latest-migration", "（等队友 B）")
        resp = self.server.get("/api/v1/install/latest-migration")
        self.check_envelope(resp)

    def test_07_install_test_db(self) -> None:
        self.require("POST", "/api/v1/install/test-db", "（等队友 B）")
        resp = self.server.post("/api/v1/install/test-db", {})
        self.check_envelope(resp)

    def test_08_static_frontend(self) -> None:
        """前端产物存在时必须能取到 index.html；不存在则明确跳过。"""
        if not self.server.static_index:
            self.skipTest("web/static/index.html 尚不存在（等队友 C）")
        resp = self.server.get("/")
        self.assertEqual(resp.status, 200, msg=json_dump(resp.body))
        self.assertIn(b"<", resp.raw)
        self.assertIn("text/html", resp.headers.get("content-type", ""), msg=json_dump(resp.headers))

    def test_09_public_packages(self) -> None:
        self.require("GET", "/api/v1/packages", "（等队友 A）")
        resp = self.server.get("/api/v1/packages")
        self.check_envelope(resp)
        data = resp.data or {}
        items = data.get("items") if isinstance(data, dict) else data
        self.assertTrue(items, msg="迁移里 seed 了 4 个套餐，不该为空：" + json_dump(data))
        print(f"\n    packages -> {[i.get('code') for i in items]}")


class TestColdStartInstall(SmokeBase):
    """冷启动：空库 → install/check（未安装）→ install/execute → 管理员可登录。"""

    def test_cold_start_install_then_admin_login(self) -> None:
        self.require("POST", "/api/v1/install/execute", "（等队友 B）")
        before = self.server.get("/api/v1/install/check")
        self.check_envelope(before)
        self.assertFalse((before.data or {}).get("installed"), msg=json_dump(before.body))

        resp = self.server.post("/api/v1/install/execute",
                                {"username": "admin", "password": ADMIN_PASSWORD})
        self.check_envelope(resp)
        print(f"\n    install/execute -> {json_dump(resp.body)}")

        row = self.server.db_one("SELECT username, role FROM users WHERE role='admin' ORDER BY id LIMIT 1")
        self.assertIsNotNone(row, msg="install/execute 之后数据库里必须有一个管理员")
        self.assertEqual(row["role"], "admin")

        after = self.server.get("/api/v1/install/check")
        self.assertTrue((after.data or {}).get("installed"), msg=json_dump(after.body))

        # 二次调用必须被拒（契约：仅当尚无管理员时允许）
        again = self.server.post("/api/v1/install/execute", {"username": "admin2",
                                                            "password": ADMIN_PASSWORD})
        self.check_envelope(again, 409)

        if self.server.has_route("POST", "/api/v1/auth/admin/login"):
            login = self.server.post("/api/v1/auth/admin/login",
                                     {"identifier": row["username"], "password": ADMIN_PASSWORD})
            self.check_envelope(login)
            self.assertTrue(self.server.access_token_of(login), msg=json_dump(login.body))


class TestUserJourney(SmokeBase):
    """注册 → 登录 → 账号 → 任务 → 记录 的完整用户链路。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.token = ""
        cls.user_id = 0

    def _register_and_login(self) -> str:
        self.require("POST", "/api/v1/auth/user/register", "（等队友 A）")
        reg = self.server.register("journey")
        self.check_envelope(reg)
        token = self.server.access_token_of(reg)
        self.assertTrue(token, msg="注册即登录，必须返回令牌：" + json_dump(reg.body))
        self.__class__.token = token
        self.__class__.user_id = int((reg.data or {}).get("user", {}).get("id") or 0)
        # 注册本身不送套餐；后面的账号/任务链路需要额度，这里按「用户已购月卡」挂上。
        # 没有套餐时的 402 行为单独由 test_06 验证。
        row = self.server.db_one("SELECT * FROM users WHERE id=?", (self.__class__.user_id,))
        if row:
            self.server.grant_package(row)
        return token

    def test_01_register_login_flow(self) -> None:
        token = self._register_and_login()
        self.require("GET", "/api/v1/user/info", "（等队友 A）")
        info = self.server.get("/api/v1/user/info", token=token)
        self.check_envelope(info)
        data = info.data or {}
        self.assertEqual(data.get("username"), "journey", msg=json_dump(data))
        self.assertIn("quota", data, msg=json_dump(data))

        # 重复注册同名用户必须 409
        dup = self.server.register("journey")
        self.check_envelope(dup, 409)

        # 密码错误必须 401 且错误码在白名单里
        self.require("POST", "/api/v1/auth/user/login", "（等队友 A）")
        bad = self.server.login("journey", "WrongPass!2026")
        self.check_envelope(bad, 401)
        ok = self.server.login("journey", USER_PASSWORD)
        self.check_envelope(ok)
        self.assertTrue(self.server.access_token_of(ok), msg=json_dump(ok.body))

    def test_02_account_and_task_flow(self) -> None:
        token = self.token or self._register_and_login()
        self.require("POST", "/api/v1/douyin/accounts", "（等队友 A）")
        created = self.server.post("/api/v1/douyin/accounts", {"remark": "e2e 小号"}, token=token)
        self.check_envelope(created)
        account_id = int((created.data or {}).get("account_id") or 0)
        self.assertGreater(account_id, 0, msg=json_dump(created.body))

        listing = self.server.get("/api/v1/douyin/accounts", token=token)
        self.check_envelope(listing)
        data = listing.data or {}
        self.assertIn("items", data, msg=json_dump(data))
        self.assertEqual(data.get("page"), 1, msg=json_dump(data))

        detail = self.server.get(f"/api/v1/douyin/accounts/{account_id}", token=token)
        self.check_envelope(detail)

        self.require("POST", "/api/v1/tasks", "（等队友 A）")
        task = self.server.post("/api/v1/tasks", {
            "account_id": account_id,
            "name": "e2e 自动续火花",
            "time_of_day": "08:30",
            "repeat_mode": "daily",
            "messages": ["火花不能断 🔥", "打卡 ✨"],
            "send_gap_seconds": {"min": 3, "max": 9},
        }, token=token)
        self.check_envelope(task)
        task_id = int((task.data or {}).get("id") or 0)
        self.assertGreater(task_id, 0, msg=json_dump(task.body))

        preview = self.server.post(f"/api/v1/tasks/{task_id}/preview", {}, token=token)
        self.check_envelope(preview)

        toggled = self.server.post(f"/api/v1/tasks/{task_id}/toggle", {}, token=token)
        self.check_envelope(toggled)

        runs = self.server.get(f"/api/v1/tasks/{task_id}/runs", token=token)
        self.check_envelope(runs)

    def test_02b_friends_batch_delete(self) -> None:
        """好友批量删除：只删自己的、非法输入给 400、空选择给 400。

        真实场景：从抖音同步会导入一堆「纯数字昵称」的陌生会话，用户需要一个
        「全选 → 批量删除」的入口，逐个点删除太痛苦。
        """
        token = self.token or self._register_and_login()
        self.require("POST", "/api/v1/friends/batch-delete", "（好友批量删除接口）")
        account = self.server.post("/api/v1/douyin/accounts", {"remark": "批量删除测试"}, token=token)
        self.check_envelope(account)
        account_id = int((account.data or {}).get("account_id") or 0)
        self.assertGreater(account_id, 0, msg=json_dump(account.body))
        self.addCleanup(lambda: self.server.delete(
            f"/api/v1/douyin/accounts/{account_id}", token=token))

        imported = self.server.post("/api/v1/friends/import", {
            "account_id": account_id, "text": "批量A\n批量B\n批量C",
        }, token=token)
        self.check_envelope(imported)
        listing = self.server.get(f"/api/v1/friends?account_id={account_id}&page_size=100", token=token)
        self.check_envelope(listing)
        ids = [int(f["id"]) for f in ((listing.data or {}).get("items") or [])]
        self.assertGreaterEqual(len(ids), 3, msg=json_dump(listing.body))

        # 空选择 → 400
        empty = self.server.post("/api/v1/friends/batch-delete", {"ids": []}, token=token)
        self.assertEqual(empty.status, 400, msg=json_dump(empty.body))

        # 正常批量删两个
        deleted = self.server.post("/api/v1/friends/batch-delete", {"ids": ids[:2]}, token=token)
        self.check_envelope(deleted)
        self.assertEqual(int((deleted.data or {}).get("deleted") or 0), 2,
                         msg=json_dump(deleted.body))

        after = self.server.get(f"/api/v1/friends?account_id={account_id}&page_size=100", token=token)
        self.check_envelope(after)
        remaining = [int(f["id"]) for f in ((after.data or {}).get("items") or [])]
        self.assertNotIn(ids[0], remaining, "删掉的好友不该还在列表里")
        self.assertIn(ids[2], remaining, "没删的好友必须保留")

        # 别人的 id 不能删（同一批里混入不存在的 id 也只算实际删除数）
        ghost = self.server.post("/api/v1/friends/batch-delete", {"ids": [999999999]}, token=token)
        self.check_envelope(ghost)
        self.assertEqual(int((ghost.data or {}).get("deleted") or 0), 0,
                         msg=json_dump(ghost.body))

        # 收尾：删掉本用例建的账号（否则套餐的「账号数量上限」会被后续用例打爆 → 402）

    def test_03_records_pagination(self) -> None:
        token = self.token or self._register_and_login()
        self.require("GET", "/api/v1/douyin/task-records", "（等队友 A）")
        resp = self.server.get("/api/v1/douyin/task-records?page=1&page_size=10", token=token)
        self.check_envelope(resp)
        data = resp.data or {}
        for key in ("items", "total", "page", "page_size", "pages"):
            self.assertIn(key, data, msg="分页响应结构不符合契约 §0：" + json_dump(data))
        self.assertEqual(data["page"], 1, msg=json_dump(data))
        self.assertEqual(data["page_size"], 10, msg=json_dump(data))

    def test_04_unauthorized_without_token(self) -> None:
        self.require("GET", "/api/v1/user/info", "（等队友 A）")
        resp = self.server.get("/api/v1/user/info")
        self.check_envelope(resp, 401)
        self.assertEqual(resp.code, "unauthorized", msg=json_dump(resp.body))
        bad = self.server.get("/api/v1/user/info", token="not.a.jwt")
        self.check_envelope(bad, 401)

    def test_05_cross_user_isolation(self) -> None:
        """越权访问他人资源必须 404/403，不能 200。"""
        token_a = self.token or self._register_and_login()
        self.require("POST", "/api/v1/douyin/accounts", "（等队友 A）")
        account = self.server.post("/api/v1/douyin/accounts", {"remark": "A 的账号"}, token=token_a)
        self.check_envelope(account)
        account_id = int(((account.data or {}).get("account_id")) or 0)
        if not account_id:
            self.skipTest("账号创建接口未返回 account_id")

        user_b = self.server.create_user(username="intruder", password=USER_PASSWORD)
        token_b = self.server.token_for(user_b)

        for method, path in (("GET", f"/api/v1/douyin/accounts/{account_id}"),
                             ("PATCH", f"/api/v1/douyin/accounts/{account_id}"),
                             ("DELETE", f"/api/v1/douyin/accounts/{account_id}")):
            with self.subTest(method=method):
                resp = self.server.call(method, path, {"remark": "hack"} if method != "GET" else None,
                                        token=token_b)
                self.assertIn(resp.status, (403, 404),
                              msg=f"越权访问必须被拒：{method} {path} -> {resp.status} "
                                  + json_dump(resp.body))

        # 管理员越权访问用户端资源由 /admin/* 承担；用户访问 /admin/* 必须 403
        self.require("GET", "/api/v1/admin/stats", "（等队友 B）")
        resp = self.server.get("/api/v1/admin/stats", token=token_b)
        self.assertIn(resp.status, (403, 401), msg=json_dump(resp.body))
        self.assertEqual(resp.code, "forbidden", msg=json_dump(resp.body))

    def test_07_daily_task_limit_is_429_with_code(self) -> None:
        """契约 §0：额度用尽是 429 + code=``task_daily_limit``（前端**按 code 分支**）。

        Lead 已裁定：不改成 402。所以这里断言的是 code，而不是只看 HTTP 状态码——
        429 还可能是 ``too_many_requests``（限流）或 ``locked``（登录锁定），
        前端要能区分「稍后重试」与「去升级套餐」。
        """
        self.require("POST", "/api/v1/douyin/accounts", "（等队友 A）")
        self.require("POST", "/api/v1/tasks", "（等队友 A）")
        self.require("POST", "/api/v1/tasks/1/run", "（等队友 A）")

        # 本用例验证的是「有每日次数上限」时的契约行为，所以显式关掉不限次模式
        # （自托管默认不限次，不关的话额度永远拦不住，429 断言必然失败）。
        from app.config import get_settings                 # noqa: PLC0415
        settings = get_settings()
        original_unlimited = getattr(settings, "unlimited_task_runs", True)
        self.addCleanup(setattr, settings, "unlimited_task_runs", original_unlimited)
        settings.unlimited_task_runs = False

        from app import db                                  # noqa: PLC0415
        from app.common import timeutil                     # noqa: PLC0415

        # 体验卡：1 个账号、每天 1 次 —— 上限最小，最容易把额度打满
        user = self.server.create_user(username="quota429", package="trial")
        token = self.server.token_for(user)
        account = self.server.post("/api/v1/douyin/accounts", {"remark": "额度测试"}, token=token)
        self.check_envelope(account)
        account_id = int((account.data or {}).get("account_id") or 0)
        if not account_id:
            self.skipTest("账号创建接口未返回 account_id")
        # 接口先查「账号是否在线」再查额度（顺序合理：离线账号根本没法跑），
        # 所以这里把账号置为在线，才能走到额度分支。
        db.update("douyin_accounts", {"status": "online", "updated_at": timeutil.utc_now_str()},
                  "id=?", (account_id,))

        task = self.server.post("/api/v1/tasks",
                                {"account_id": account_id, "name": "额度测试任务"}, token=token)
        self.check_envelope(task)
        task_id = int((task.data or {}).get("id") or 0)
        if not task_id:
            self.skipTest("任务创建接口未返回 id")

        # 造一条今天的执行记录把当天额度用掉（等价于「今天已经跑过一次」）
        now = timeutil.utc_now_str()
        db.insert("task_runs", {
            "task_id": task_id, "user_id": user["id"], "account_id": account_id,
            "run_date": timeutil.local_date_str(), "trigger_type": "manual", "status": "success",
            "started_at": now, "finished_at": now, "created_at": now,
        })

        resp = self.server.post(f"/api/v1/tasks/{task_id}/run", {}, token=token)
        self.assertEqual(resp.status, 429, msg="额度用尽必须 429：" + json_dump(resp.body))
        self.assertEqual(resp.code, "task_daily_limit", msg="契约点：code 必须是 task_daily_limit："
                                                          + json_dump(resp.body))

        # 打开不限次模式后：额度摘要要明确告诉前端「不限次」，且不再拦执行
        settings.unlimited_task_runs = True
        summary = self.server.get("/api/v1/user/packages/limit-status", token=token)
        self.check_envelope(summary)
        data = summary.data or {}
        self.assertIs(data.get("daily_task_unlimited"), True,
                      msg="不限次模式必须回传 daily_task_unlimited=true：" + json_dump(resp.body))
        self.assertEqual(int(data.get("daily_task_limit") or 0), 0,
                         msg="不限次时 daily_task_limit 应为 0（前端据此显示 ∞）")

    def test_06_quota_gate_without_package(self) -> None:
        """没有生效套餐的用户不能加账号：必须 402 no_active_package，不能悄悄放行。"""
        self.require("POST", "/api/v1/douyin/accounts", "（等队友 A）")
        naked = self.server.create_user(username="nopackage", package=None)
        token = self.server.token_for(naked)
        resp = self.server.post("/api/v1/douyin/accounts", {"remark": "无套餐"}, token=token)
        self.check_envelope(resp, 402)
        self.assertEqual(resp.code, "no_active_package", msg=json_dump(resp.body))

        # 挂上套餐后同一请求必须放行（证明拦的是套餐，不是别的）
        self.server.grant_package(naked, "trial")
        retry = self.server.post("/api/v1/douyin/accounts", {"remark": "有套餐了"}, token=token)
        self.check_envelope(retry)


class TestCardAndOrderFlow(SmokeBase):
    """卡密与订单：管理员发卡 → 用户兑换 → 重复兑换 409 → 禁卡 → 下单。"""

    def test_cardkey_redeem_flow(self) -> None:
        self.require("POST", "/api/v1/admin/card-keys/generate", "（等队友 B）")
        self.require("POST", "/api/v1/user/card-keys/redeem", "（等队友 A/B）")
        admin_token = self.login_admin()
        user = self.server.create_user(username="buyer", password=USER_PASSWORD)
        user_token = self.server.token_for(user)

        generated = self.server.post("/api/v1/admin/card-keys/generate", {
            "package_code": "monthly", "card_type": "time", "days": 30, "count": 2,
            "use_limit": 1, "amount_cents": 0, "note": "e2e 批次",
        }, token=admin_token)
        self.check_envelope(generated)
        codes = (generated.data or {}).get("codes") or []
        self.assertEqual(len(codes), 2, msg=json_dump(generated.body))
        batch_no = (generated.data or {}).get("batch_no") or ""
        self.assertTrue(batch_no, msg=json_dump(generated.body))

        first = self.server.post("/api/v1/user/card-keys/redeem", {"code": codes[0]}, token=user_token)
        self.check_envelope(first)
        self.assertIn("kind", first.data or {}, msg=json_dump(first.body))

        again = self.server.post("/api/v1/user/card-keys/redeem", {"code": codes[0]}, token=user_token)
        self.check_envelope(again, 409)
        self.assertEqual(again.code, "card_used", msg=json_dump(again.body))

        fake = self.server.post("/api/v1/user/card-keys/redeem", {"code": "NOT-A-REAL-CARD"}, token=user_token)
        self.check_envelope(fake, 400)
        self.assertEqual(fake.code, "card_invalid", msg=json_dump(fake.body))

        balance_log = self.server.db_one(
            "SELECT COUNT(*) AS n FROM card_redemptions WHERE user_id=?", (user["id"],))
        self.assertGreaterEqual(int(balance_log["n"]), 1, msg="兑换必须写 card_redemptions 流水")

    def test_order_and_pay_methods(self) -> None:
        user = self.server.create_user(username="payer", password=USER_PASSWORD)
        token = self.server.token_for(user)

        self.require("GET", "/api/v1/user/packages", "（等队友 A）")
        pkgs = self.server.get("/api/v1/user/packages", token=token)
        self.check_envelope(pkgs)

        self.require("GET", "/api/v1/user/packages/pay-methods", "（等队友 A/B）")
        methods = self.server.get("/api/v1/user/packages/pay-methods", token=token)
        self.check_envelope(methods)
        data = methods.data or {}
        self.assertIn("online_pay_enabled", data, msg=json_dump(data))
        channels = [i.get("channel") for i in (data.get("items") or [])]
        self.assertIn("card", channels, msg="pay.enabled=0 时至少要有卡密渠道：" + json_dump(data))
        # 未配置在线支付时不允许出现「假按钮」：网关类渠道必须 disabled。
        # 注意 card / balance / manual 是站内渠道，不受 online_pay_enabled 影响。
        gateway_channels = {"easypay", "alipay", "wechat"}
        if not data.get("online_pay_enabled"):
            for item in data.get("items") or []:
                if item.get("channel") in gateway_channels:
                    self.assertFalse(item.get("enabled"),
                                     msg="在线支付未启用，网关渠道却是 enabled："
                                         + json_dump(item))

        self.require("POST", "/api/v1/user/packages/purchase", "（等队友 A）")
        listed = self.server.get("/api/v1/packages")
        packages = (listed.data or {}).get("items") or []
        target = next((p for p in packages if p.get("code") == "monthly"), None)
        if not target:
            self.skipTest("套餐列表里没有 monthly，跳过下单链路")
        order = self.server.post("/api/v1/user/packages/purchase",
                                 {"package_id": target["id"], "channel": "card"}, token=token)
        self.check_envelope(order)
        order_no = (order.data or {}).get("order_no") or ""
        self.assertTrue(order_no, msg=json_dump(order.body))

        detail = self.server.get(f"/api/v1/user/orders/{order_no}", token=token)
        self.check_envelope(detail)
        cancel = self.server.post(f"/api/v1/user/orders/{order_no}/cancel", {}, token=token)
        self.assertIn(cancel.status, (200, 409), msg=json_dump(cancel.body))


class TestAdminSurface(SmokeBase):
    """管理端接口：统计、用户列表、卡密统计、审计日志。"""

    def test_admin_endpoints(self) -> None:
        self.require("GET", "/api/v1/admin/stats", "（等队友 B）")
        admin_token = self.login_admin()
        user = self.server.create_user(username="plain", password=USER_PASSWORD)
        user_token = self.server.token_for(user)

        stats = self.server.get("/api/v1/admin/stats", token=admin_token)
        self.check_envelope(stats)
        data = stats.data or {}
        for key in ("users", "orders", "tasks", "cards"):
            self.assertIn(key, data, msg="统计结构不符合契约 §7：" + json_dump(data))

        for path in ("/api/v1/admin/users", "/api/v1/admin/card-keys/stats",
                     "/api/v1/admin/system-environment"):
            if not self.server.has_route("GET", path):
                continue
            with self.subTest(path=path):
                resp = self.server.get(path, token=admin_token)
                self.check_envelope(resp)
                denied = self.server.get(path, token=user_token)
                self.assertIn(denied.status, (401, 403), msg=json_dump(denied.body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
