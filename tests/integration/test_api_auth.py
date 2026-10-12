"""API 认证链路集成测试：``TestClient`` + 真 MySQL / Redis（不需要 ES / LLM）。

这是验收清单里"curl 能跑通"那条的自动化版本：注册 → 登录（拿到 HttpOnly Cookie）
→ /me → 改密码 → **旧 Cookie 立刻失效** → 新密码可登录。

数据卫生：账号一律 ``api_test_<随机>``，teardown 物理删除；
会话在登出/改密码时即被吊销，不会在 Redis 里留垃圾。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db import get_conn

pytestmark = pytest.mark.integration

OLD_PASSWORD = "OldPass_123!"
NEW_PASSWORD = "NewPass_456!"
COOKIE_NAME = "rag_session"


@pytest.fixture()
def client():
    """独立 client（``with`` 会触发 lifespan：ES 自检失败也只是降级，不影响本文件）。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def api_user():
    """用真实注册接口建临时账号，用完物理删除。"""
    username = f"api_test_{uuid.uuid4().hex[:12]}"
    with TestClient(app) as c:
        resp = c.post(
            "/api/auth/register", json={"username": username, "password": OLD_PASSWORD}
        )
        assert resp.status_code == 201, resp.text
        user_uuid = resp.json()["user_uuid"]
    try:
        yield {"username": username, "user_uuid": user_uuid}
    finally:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE user_uuid = %s", (user_uuid,))


def _login(client, username, password):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _inject_token(client, token):
    """把 token 直接塞进 cookie jar，模拟"token 已被人复制走"的场景。

    为什么不用 ``client.get(..., cookies={...})``：starlette 已把 per-request cookies
    标记为弃用；更重要的是——必须**先断言注入后能访问成功**，否则后面的 401
    可能只是"cookie 根本没带上"造成的假通过。
    """
    client.cookies.set(COOKIE_NAME, token, domain="testserver", path="/")


class TestRegister:
    def test_register_creates_user(self, client, api_user):
        assert api_user["user_uuid"]

    def test_duplicate_username_returns_409(self, client, api_user):
        resp = client.post(
            "/api/auth/register",
            json={"username": api_user["username"], "password": OLD_PASSWORD},
        )
        assert resp.status_code == 409
        assert "已存在" in resp.json()["detail"]

    def test_short_password_returns_400(self, client):
        resp = client.post(
            "/api/auth/register",
            json={"username": f"api_test_{uuid.uuid4().hex[:8]}", "password": "short"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "密码至少 8 位"

    def test_client_supplied_role_is_ignored(self, client):
        """★ 安全：body 里的 role 必须被忽略，否则任何人都能把自己注册成管理员。"""
        username = f"api_test_{uuid.uuid4().hex[:12]}"
        resp = client.post(
            "/api/auth/register",
            json={"username": username, "password": OLD_PASSWORD, "role": "admin"},
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == "user"
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(
                    "SELECT role FROM users WHERE user_uuid = %s",
                    (resp.json()["user_uuid"],),
                )
                assert cur.fetchone()["role"] == "user"
                cur.execute(
                    "DELETE FROM users WHERE user_uuid = %s",
                    (resp.json()["user_uuid"],),
                )


class TestLoginAndSession:
    def test_login_sets_httponly_samesite_cookie(self, client, api_user):
        resp = _login(client, api_user["username"], OLD_PASSWORD)

        assert resp.status_code == 200
        set_cookie = resp.headers["set-cookie"]
        assert "HttpOnly" in set_cookie, "token 必须 HttpOnly，否则 XSS 能直接偷走"
        assert "SameSite=lax" in set_cookie
        assert client.cookies.get(COOKIE_NAME)

    def test_me_works_after_login(self, client, api_user):
        _login(client, api_user["username"], OLD_PASSWORD)

        resp = client.get("/api/auth/me")

        assert resp.status_code == 200
        assert resp.json()["username"] == api_user["username"]
        assert resp.json()["role"] == "user"

    def test_me_without_session_is_401(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "未登录或会话已过期，请重新登录"

    def test_wrong_password_and_unknown_user_share_one_message(self, client, api_user):
        """不区分"密码错"与"用户不存在"：避免账号枚举。"""
        wrong = _login(client, api_user["username"], "WrongPass_999!")
        unknown = _login(client, "api_test_nobody", OLD_PASSWORD)

        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["detail"] == unknown.json()["detail"] == "用户名或密码错误"

    def test_logout_revokes_even_with_stale_cookie(self, client, api_user):
        """登出必须**服务端吊销**：只删浏览器 Cookie 的话，被复制的 token 依然可用。"""
        _login(client, api_user["username"], OLD_PASSWORD)
        token = client.cookies.get(COOKIE_NAME)
        assert token

        # 先用注入的 token 证明这条通路有效（否则后面的 401 可能只是"cookie 没带上"）
        _inject_token(client, token)
        assert client.get("/api/auth/me").status_code == 200

        assert client.post("/api/auth/logout").status_code == 200

        # 服务端已吊销：把旧 token 重新塞回去，也必须 401
        _inject_token(client, token)
        assert client.get("/api/auth/me").status_code == 401


class TestChangePassword:
    def test_change_rotates_password_and_kills_all_sessions(self, client, api_user):
        """改密码的完整语义：旧凭据立即失效 + 所有会话被吊销。"""
        _login(client, api_user["username"], OLD_PASSWORD)
        token = client.cookies.get(COOKIE_NAME)
        assert token
        _inject_token(client, token)
        assert client.get("/api/auth/me").status_code == 200  # 注入通路有效

        resp = client.post(
            "/api/auth/password",
            json={
                "old_password": OLD_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )

        assert resp.status_code == 200, resp.text
        assert "已退出全部" in resp.json()["message"]
        # 1) 服务端已吊销：把旧 token 重新塞回去也必须 401（不只是清了浏览器 Cookie）
        _inject_token(client, token)
        assert client.get("/api/auth/me").status_code == 401
        # 2) 旧密码作废、新密码可用
        with TestClient(app) as fresh:
            assert _login(fresh, api_user["username"], OLD_PASSWORD).status_code == 401
            assert _login(fresh, api_user["username"], NEW_PASSWORD).status_code == 200

    @pytest.mark.parametrize(
        "body,expected_msg",
        [
            (
                {"old_password": "WrongOld_999!", "new_password": NEW_PASSWORD,
                 "confirm_password": NEW_PASSWORD},
                "当前密码不正确",
            ),
            (
                {"old_password": OLD_PASSWORD, "new_password": OLD_PASSWORD,
                 "confirm_password": OLD_PASSWORD},
                "新密码不能与当前密码相同",
            ),
            (
                {"old_password": OLD_PASSWORD, "new_password": "short",
                 "confirm_password": "short"},
                "密码至少 8 位",
            ),
            (
                {"old_password": OLD_PASSWORD, "new_password": NEW_PASSWORD,
                 "confirm_password": "Mismatch_1!"},
                "两次输入的新密码不一致",
            ),
        ],
    )
    def test_rejected_change_keeps_old_password_working(
        self, client, api_user, body, expected_msg
    ):
        """被拒绝的路径一律不许写库（否则"提示失败但密码已经变了"）。"""
        _login(client, api_user["username"], OLD_PASSWORD)

        resp = client.post("/api/auth/password", json=body)

        assert resp.status_code == 400
        assert resp.json()["detail"] == expected_msg
        with TestClient(app) as fresh:
            assert _login(fresh, api_user["username"], OLD_PASSWORD).status_code == 200

    def test_change_requires_login(self, client):
        resp = client.post(
            "/api/auth/password",
            json={
                "old_password": OLD_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )
        assert resp.status_code == 401


class TestScopeEnforcementOverHttp:
    """HTTP 层确认 scope 校验真的接上了（单元矩阵见 tests/unit/test_scope_auth.py）。"""

    def test_foreign_private_scope_is_403(self, client, api_user):
        _login(client, api_user["username"], OLD_PASSWORD)

        for path in ("/api/files", "/api/history"):
            resp = client.get(path, params={"scope": "private:someone-else"})
            assert resp.status_code == 403, f"{path} 未拦住越权 scope"

    def test_invalid_scope_is_400(self, client, api_user):
        _login(client, api_user["username"], OLD_PASSWORD)

        resp = client.get("/api/files", params={"scope": "nonsense"})

        assert resp.status_code == 400

    def test_own_private_scope_is_allowed(self, client, api_user):
        _login(client, api_user["username"], OLD_PASSWORD)

        resp = client.get(
            "/api/files", params={"scope": f"private:{api_user['user_uuid']}"}
        )

        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_files_requires_login(self, client):
        assert client.get("/api/files", params={"scope": "public"}).status_code == 401


class TestHealth:
    def test_health_is_public_and_cheap(self, client):
        resp = client.get("/api/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "rag-api"

