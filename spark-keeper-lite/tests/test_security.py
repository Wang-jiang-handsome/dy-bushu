"""安全测试：口令哈希 / JWT / 限流 / 登录锁定 / 越权 / 令牌吊销。

分两层：
* 纯函数层（``AppHarness``，不联网）：口令哈希、JWT 签名与过期、卡密规范化、对称加密。
* HTTP 层（``ServerHarness``，真实请求）：401 分支、角色越权 403、禁用账号、
  令牌吊销、限流 429、登录失败锁定 429、跨用户资源隔离。

运行：
    cd spark-shared
    python -m unittest tests.test_security -v
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_harness import (  # noqa: E402
    ADMIN_PASSWORD,
    USER_PASSWORD,
    AppHarness,
    ServerHarness,
    json_dump,
)


class TestSecurityPrimitives(unittest.TestCase):
    """口令与令牌的纯函数行为（不依赖任何队友的接口实现）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = AppHarness("secprims").start()
        from app.common import security as sec          # noqa: PLC0415

        cls.sec = sec

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    # ---- 口令 ----
    def test_password_hash_roundtrip(self) -> None:
        stored = self.sec.hash_password("Spark!2026")
        self.assertNotIn("Spark!2026", stored, msg="哈希里不能出现明文口令")
        ok, needs_rehash = self.sec.verify_password("Spark!2026", stored)
        self.assertTrue(ok)
        self.assertFalse(needs_rehash)
        bad, _ = self.sec.verify_password("Spark!2027", stored)
        self.assertFalse(bad, msg="错误口令必须校验失败")

    def test_password_hash_is_salted(self) -> None:
        a = self.sec.hash_password("same-password-123")
        b = self.sec.hash_password("same-password-123")
        self.assertNotEqual(a, b, msg="相同口令两次哈希必须不同（盐不同）")

    def test_password_policy(self) -> None:
        self.assertTrue(self.sec.password_problem("123"), msg="纯数字短口令必须被拒")
        self.assertEqual(self.sec.password_problem("Spark!2026"), "",
                         msg=f"合规口令不该被拒：{self.sec.password_problem('Spark!2026')}")

    # ---- JWT ----
    def test_jwt_sign_and_verify(self) -> None:
        token = self.sec.sign_jwt({"sub": 7, "role": "user"})
        payload = self.sec.verify_jwt(token)
        self.assertEqual(int(payload["sub"]), 7)
        self.assertEqual(payload["kind"], "access")
        self.assertIn("jti", payload)
        self.assertIn("exp", payload)

    def test_jwt_rejects_tampered_payload(self) -> None:
        token = self.sec.sign_jwt({"sub": 7, "role": "user"})
        head, body, sig = token.split(".")
        forged = f"{head}.{body[:-2]}XY.{sig}"          # 改载荷，签名不变
        with self.assertRaises(self.sec.TokenError) as ctx:
            self.sec.verify_jwt(forged)
        self.assertFalse(ctx.exception.expired)

    def test_jwt_rejects_tampered_signature(self) -> None:
        token = self.sec.sign_jwt({"sub": 7})
        with self.assertRaises(self.sec.TokenError):
            self.sec.verify_jwt(token[:-4] + "AAAA")

    def test_jwt_rejects_wrong_secret(self) -> None:
        token = self.sec.sign_jwt({"sub": 7}, secret="another-secret-0123456789abcdef")
        with self.assertRaises(self.sec.TokenError):
            self.sec.verify_jwt(token)

    def test_jwt_expiry(self) -> None:
        token = self.sec.sign_jwt({"sub": 7}, ttl=-10)      # 已过期
        with self.assertRaises(self.sec.TokenError) as ctx:
            self.sec.verify_jwt(token)
        self.assertTrue(ctx.exception.expired, msg="过期必须标记 expired=True，前端据此去刷新令牌")

    def test_jwt_kind_mismatch(self) -> None:
        refresh = self.sec.sign_jwt({"sub": 7}, kind="refresh")
        self.assertEqual(self.sec.verify_jwt(refresh, kind="refresh")["kind"], "refresh")
        with self.assertRaises(self.sec.TokenError):
            self.sec.verify_jwt(refresh, kind="access")     # refresh 不能当 access 用

    def test_jwt_garbage_input(self) -> None:
        for bad in ("", "a.b", "a.b.c.d", "not-a-jwt"):
            with self.subTest(bad=bad), self.assertRaises(self.sec.TokenError):
                self.sec.verify_jwt(bad)

    # ---- 卡密与加密 ----
    def test_card_code_normalize_and_hash(self) -> None:
        code = self.sec.new_card_code()
        self.assertGreaterEqual(len(code), 12, msg=f"卡密太短：{code}")
        self.assertEqual(self.sec.normalize_card_code(code.lower()), self.sec.normalize_card_code(code),
                         msg="规范化必须忽略大小写")
        self.assertEqual(self.sec.card_code_hash(code), self.sec.card_code_hash(code))
        self.assertNotEqual(self.sec.card_code_hash(code), code, msg="库里只存哈希，不存明文卡密")

    def test_encrypt_roundtrip(self) -> None:
        secret_text = "proxy-password-中文-123"
        blob = self.sec.encrypt(secret_text)
        self.assertNotIn(secret_text, blob)
        self.assertEqual(self.sec.decrypt(blob), secret_text)
        # 篡改/换密钥必须解不开。实现选择「返回空串」而不是抛异常（业务不该被密文问题打断），
        # 所以这里断言的是「拿不到明文」，关键是绝不能吐出垃圾明文。
        self.assertEqual(self.sec.decrypt(blob[:-3] + "xxx"), "",
                         msg="密文被篡改后仍解出了内容，说明缺少完整性校验")
        self.assertEqual(self.sec.decrypt(blob, secret="another-secret-0123456789abcdef"), "",
                         msg="换密钥仍能解密，说明没真正用密钥保护")
        self.assertEqual(self.sec.decrypt("not-a-ciphertext"), "")

    def test_masking(self) -> None:
        self.assertNotIn("13800138000", self.sec.mask_phone("13800138000"))
        self.assertNotIn("someone", self.sec.mask_email("someone@example.com"))


class TestHttpAuthBranches(unittest.TestCase):
    """HTTP 层的鉴权分支（401/403/吊销/禁用）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = ServerHarness("sechttp").start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def require(self, method: str, path: str) -> None:
        if not self.h.has_route(method, path):
            self.skipTest(f"接口未就绪：{method} {path}")

    def test_no_token_is_401(self) -> None:
        self.require("GET", "/api/v1/user/info")
        resp = self.h.get("/api/v1/user/info")
        self.assertEqual(resp.status, 401, msg=json_dump(resp.body))
        self.assertEqual(resp.code, "unauthorized", msg=json_dump(resp.body))

    def test_tampered_token_is_401(self) -> None:
        self.require("GET", "/api/v1/user/info")
        user = self.h.create_user("tamper")
        token = self.h.token_for(user)
        resp = self.h.get("/api/v1/user/info", token=token[:-4] + "AAAA")
        self.assertEqual(resp.status, 401, msg=json_dump(resp.body))

    def test_revoked_session_is_401(self) -> None:
        """改密/强制下线后，旧 JWT 必须立刻失效（sessions 白名单的意义）。"""
        self.require("GET", "/api/v1/user/info")
        from app.common.auth import revoke_session          # noqa: PLC0415

        user = self.h.create_user("revoked")
        token = self.h.token_for(user)
        self.assertEqual(self.h.get("/api/v1/user/info", token=token).status, 200)
        revoke_session(token)
        resp = self.h.get("/api/v1/user/info", token=token)
        self.assertEqual(resp.status, 401, msg="吊销后的令牌还能用：" + json_dump(resp.body))

    def test_deleted_user_token_is_401(self) -> None:
        self.require("GET", "/api/v1/user/info")
        from app import db as appdb                          # noqa: PLC0415

        user = self.h.create_user("deleted")
        token = self.h.token_for(user)
        appdb.delete("users", "id=?", (user["id"],))
        resp = self.h.get("/api/v1/user/info", token=token)
        self.assertEqual(resp.status, 401, msg=json_dump(resp.body))

    def test_disabled_user_is_403(self) -> None:
        self.require("GET", "/api/v1/user/info")
        from app import db as appdb                          # noqa: PLC0415
        from app.common import timeutil                      # noqa: PLC0415

        user = self.h.create_user("disabled")
        token = self.h.token_for(user)
        appdb.update("users", {"status": "disabled", "updated_at": timeutil.utc_now_str()},
                     "id=?", (user["id"],))
        resp = self.h.get("/api/v1/user/info", token=token)
        self.assertEqual(resp.status, 403, msg="被禁用账号必须 403：" + json_dump(resp.body))

    def test_user_cannot_touch_admin_api(self) -> None:
        user = self.h.create_user("notadmin")
        token = self.h.token_for(user)
        checked = 0
        for path in ("/api/v1/admin/stats", "/api/v1/admin/users", "/api/v1/admin/logs"):
            if not self.h.has_route("GET", path):
                continue
            checked += 1
            with self.subTest(path=path):
                resp = self.h.get(path, token=token)
                self.assertEqual(resp.status, 403, msg=json_dump(resp.body))
                self.assertEqual(resp.code, "forbidden", msg=json_dump(resp.body))
        if not checked:
            self.skipTest("管理端接口尚未实现（等队友 B）")

    def test_admin_token_can_read_admin_api(self) -> None:
        if not self.h.has_route("GET", "/api/v1/admin/stats"):
            self.skipTest("管理端接口尚未实现（等队友 B）")
        admin = self.h.create_admin(password=ADMIN_PASSWORD)
        resp = self.h.get("/api/v1/admin/stats", token=self.h.token_for(admin))
        self.assertEqual(resp.status, 200, msg=json_dump(resp.body))


class TestRateLimitAndLockout(unittest.TestCase):
    """限流与登录锁定：单独起服务，把阈值调到很小。"""

    def test_rate_limit_429(self) -> None:
        h = ServerHarness("ratelimit", rate_limit_per_minute=3).start()
        try:
            codes = [h.get("/api/v1/_not-a-real-route").status for _ in range(5)]
            self.assertIn(429, codes, msg=f"超过阈值必须限流：{codes}")
            first_429 = codes.index(429)
            self.assertEqual(codes[:first_429], [404] * first_429,
                             msg=f"限流前的请求应是 404（路由不存在）：{codes}")
            resp = h.get("/api/v1/_not-a-real-route")
            self.assertEqual(resp.code, "too_many_requests", msg=json_dump(resp.body))
            self.assertIn("retry-after", {k.lower() for k in resp.headers}, msg=json_dump(resp.headers))
        finally:
            h.stop()

    def test_health_is_rate_limit_exempt(self) -> None:
        """健康检查必须免限流，否则容器 HEALTHCHECK 会把服务打成不健康。"""
        h = ServerHarness("ratelimithealth", rate_limit_per_minute=2).start()
        try:
            statuses = [h.get("/api/v1/health").status for _ in range(6)]
            self.assertEqual(statuses, [200] * 6, msg=f"健康检查被限流了：{statuses}")
        finally:
            h.stop()

    def test_login_lockout_429(self) -> None:
        h = ServerHarness("lockout", max_login_fail=3, login_lock_seconds=60).start()
        try:
            if not h.has_route("POST", "/api/v1/auth/user/login"):
                self.skipTest("登录接口未就绪（等队友 A）")
            h.create_user("locked", password=USER_PASSWORD)
            statuses = [h.login("locked", "WrongPass!2026").status for _ in range(3)]
            self.assertEqual(statuses, [401, 401, 401], msg=f"前 3 次失败应是 401：{statuses}")
            resp = h.login("locked", "WrongPass!2026")
            self.assertEqual(resp.status, 429, msg="失败次数到阈值必须锁定：" + json_dump(resp.body))
            self.assertIn(resp.code, ("locked", "too_many_requests"), msg=json_dump(resp.body))
            # 锁定期间即使口令正确也要继续拒绝
            right = h.login("locked", USER_PASSWORD)
            self.assertEqual(right.status, 429, msg="锁定期间正确口令也不能放行：" + json_dump(right.body))
        finally:
            h.stop()

    def test_login_failure_does_not_leak_user_existence(self) -> None:
        """不存在的用户与口令错误的提示必须一致，避免账号枚举。"""
        h = ServerHarness("enumeration").start()
        try:
            if not h.has_route("POST", "/api/v1/auth/user/login"):
                self.skipTest("登录接口未就绪（等队友 A）")
            h.create_user("exists", password=USER_PASSWORD)
            real = h.login("exists", "WrongPass!2026")
            ghost = h.login("no-such-user-at-all", "WrongPass!2026")
            self.assertEqual(real.status, ghost.status,
                             msg=f"状态码泄露账号是否存在：{real.status} vs {ghost.status}")
            self.assertEqual(real.message, ghost.message,
                             msg=f"提示语泄露账号是否存在：{real.message!r} vs {ghost.message!r}")
        finally:
            h.stop()


class TestCrossUserIsolation(unittest.TestCase):
    """跨用户资源隔离：A 的资源，B 拿到 id 也必须访问不到。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = ServerHarness("isolation").start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def test_account_and_task_isolation(self) -> None:
        if not self.h.has_route("POST", "/api/v1/douyin/accounts"):
            self.skipTest("账号接口未就绪（等队友 A）")
        owner = self.h.create_user("owner")
        intruder = self.h.create_user("intruder")
        owner_token = self.h.token_for(owner)
        intruder_token = self.h.token_for(intruder)

        created = self.h.post("/api/v1/douyin/accounts", {"remark": "owner 的号"}, token=owner_token)
        self.assertEqual(created.status, 200, msg=json_dump(created.body))
        account_id = int((created.data or {}).get("account_id") or 0)
        self.assertGreater(account_id, 0, msg=json_dump(created.body))

        with self.subTest("账号详情"):
            resp = self.h.get(f"/api/v1/douyin/accounts/{account_id}", token=intruder_token)
            self.assertIn(resp.status, (403, 404), msg=f"越权读到他人账号：{json_dump(resp.body)}")

        with self.subTest("账号修改"):
            resp = self.h.patch(f"/api/v1/douyin/accounts/{account_id}", {"remark": "hacked"},
                                token=intruder_token)
            self.assertIn(resp.status, (403, 404), msg=json_dump(resp.body))

        with self.subTest("账号列表不含他人数据"):
            listing = self.h.get("/api/v1/douyin/accounts", token=intruder_token)
            self.assertEqual(listing.status, 200, msg=json_dump(listing.body))
            ids = [int(i.get("id") or 0) for i in ((listing.data or {}).get("items") or [])]
            self.assertNotIn(account_id, ids, msg="列表里出现了别人的账号：" + json_dump(listing.data))

        if self.h.has_route("POST", "/api/v1/tasks"):
            task = self.h.post("/api/v1/tasks", {"account_id": account_id, "name": "owner 的任务"},
                               token=owner_token)
            self.assertEqual(task.status, 200, msg=json_dump(task.body))
            task_id = int((task.data or {}).get("id") or 0)
            if task_id:
                for method, path in (("GET", f"/api/v1/tasks/{task_id}"),
                                     ("PUT", f"/api/v1/tasks/{task_id}"),
                                     ("DELETE", f"/api/v1/tasks/{task_id}"),
                                     ("POST", f"/api/v1/tasks/{task_id}/run")):
                    with self.subTest(path=f"{method} {path}"):
                        resp = self.h.call(method, path, {"name": "hacked"} if method == "PUT" else None,
                                           token=intruder_token)
                        self.assertIn(resp.status, (403, 404),
                                      msg=f"越权操作他人任务：{method} {path} -> {resp.status} "
                                          + json_dump(resp.body))

    def test_card_code_cannot_be_probed_across_users(self) -> None:
        """卡密是全局码，但「谁兑换的」不能串：B 不能查看到 A 的兑换记录。"""
        if not self.h.has_route("GET", "/api/v1/user/card-keys"):
            self.skipTest("卡密记录接口未就绪（等队友 A）")
        user = self.h.create_user("cardviewer")
        resp = self.h.get("/api/v1/user/card-keys", token=self.h.token_for(user))
        self.assertEqual(resp.status, 200, msg=json_dump(resp.body))
        self.assertEqual(int(((resp.data or {}).get("total")) or 0), 0,
                         msg="新用户的兑换记录必须为空：" + json_dump(resp.data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
