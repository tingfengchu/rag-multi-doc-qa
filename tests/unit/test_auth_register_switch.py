"""``GET /api/auth/config`` 与自助注册开关（模块 2.2，离线、不连 MySQL/ES）。

为什么要给一个"布尔开关"写单测：
    "默认关闭注册"这类开关最容易退化成**假开关**——前端藏了入口，后端照样 201，
    于是"关闭注册"只是把入口藏起来（curl 直接建号，任何人就能看公共库）。
    所以从两个方向钉死：关着时必须 403 且**不碰数据库**，开着时必须真能建号。

离线：``TestClient`` 不加 ``with``（不跑 lifespan，不连 ES）；``create_user`` 换替身
（不连 MySQL）。注册开关本身是纯配置分支，正是可以离线测的部分。
"""
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers import auth as auth_router
from config import Settings, settings


@pytest.fixture()
def created(monkeypatch):
    """``create_user`` 替身：记录调用参数，避免测试碰真实 ``users`` 表。"""
    calls = []

    def fake_create_user(username, password, role="user"):
        calls.append({"username": username, "password": password, "role": role})
        return True, "uuid-new"

    monkeypatch.setattr(auth_router, "create_user", fake_create_user)
    return calls


@pytest.fixture()
def client(created):
    yield TestClient(app)


def _register(client, **extra):
    return client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "Passw0rd!", **extra},
    )


class TestConfigEndpoint:
    def test_config_requires_no_login(self, client):
        """登录页要调它 → 必须免登录；要求鉴权就成了死循环。"""
        resp = client.get("/api/auth/config")

        assert resp.status_code == 200
        assert set(resp.json()) == {"allow_register"}

    def test_config_follows_setting(self, client, monkeypatch):
        monkeypatch.setattr(settings, "allow_register", True)
        assert client.get("/api/auth/config").json()["allow_register"] is True

        monkeypatch.setattr(settings, "allow_register", False)
        assert client.get("/api/auth/config").json()["allow_register"] is False


class TestRegisterGate:
    def test_schema_default_is_closed(self):
        """代码默认值必须是 False（本机 rag_config.txt 可以覆盖，但默认必须安全）。

        测的是**字段默认值**而不是当前 ``settings``：否则开发者为了演示把开关打开后，
        这条断言会变成"演示专用失败"，最后被人删掉。
        """
        assert Settings.model_fields["allow_register"].default is False

    def test_closed_returns_403(self, client, monkeypatch):
        monkeypatch.setattr(settings, "allow_register", False)

        resp = _register(client)

        assert resp.status_code == 403
        assert "注册" in resp.json()["detail"]

    def test_closed_does_not_touch_database(self, client, created, monkeypatch):
        """关着的时候连建号逻辑都不该走到（否则等于"拒绝是拒绝，但已经插了一半"）。"""
        monkeypatch.setattr(settings, "allow_register", False)

        _register(client)

        assert created == []

    def test_enabled_creates_user(self, client, created, monkeypatch):
        monkeypatch.setattr(settings, "allow_register", True)

        resp = _register(client)

        assert resp.status_code == 201
        assert resp.json()["role"] == "user"
        assert created == [{"username": "alice", "password": "Passw0rd!", "role": "user"}]

    def test_role_in_body_is_still_ignored(self, client, created, monkeypatch):
        """开关打开也不能顺带把"角色自选"打开（那是提权，不是注册）。"""
        monkeypatch.setattr(settings, "allow_register", True)

        resp = _register(client, role="admin")

        assert resp.json()["role"] == "user"
        assert created[0]["role"] == "user"
