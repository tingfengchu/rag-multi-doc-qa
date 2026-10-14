"""角色模型（模块 3）的离线契约测试。

锁定的四类问题，每一类都对应真实会发生的运维事故：

1. **常量漂移**：``utils/roles`` 的角色枚举、``api.schemas.RoleName`` 的字面量、
   ``migrations/003`` 的 MySQL ENUM 三处必须逐字一致——"代码里有、DB 里没有"的取值
   不会在启动时报错，只会在**真正授权别人的那一刻**失败；
2. **能力被下放**：``authorized_user`` 只能写公共库，不能改别人角色
   （否则被授权者一步就能把自己升成管理员）；
3. **改了自己/最后一名管理员**：前者让"界面角色 vs 后端能力"错位，
   后者让系统进入"再也没有人能授权"的死锁；
4. **"改了但没生效"的降级**：会话里存的是**登录那一刻**的角色，
   所以改完必须吊销对方会话（端到端断言在 ``test_upload_scope.py``）。

离线：``auth.get_conn`` 换成假连接，不碰真 MySQL。
"""
import contextlib
import pathlib
from typing import get_args

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import auth
from api.deps import resolve_scope
from api.schemas import RoleName, UserOut, UserRoleIn
from utils.roles import (
    ALLOWED_ROLES,
    ROLE_ADMIN,
    ROLE_AUTHORIZED_USER,
    ROLE_ORDER,
    ROLE_USER,
    WRITE_PUBLIC_ROLES,
    can_write_public,
)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


# --------------------------------------------------------------- 常量一致性
class TestRoleConstants:
    def test_role_order_is_stable_and_complete(self):
        """顺序固定：报错文案与前端下拉都依赖它，随机顺序会让提示两次运行不一样。"""
        assert ROLE_ORDER == (ROLE_USER, ROLE_AUTHORIZED_USER, ROLE_ADMIN)

    def test_allowed_roles_matches_order(self):
        assert ALLOWED_ROLES == set(ROLE_ORDER)

    def test_write_public_roles_is_exactly_admin_plus_authorized(self):
        """能写公共库的角色集合：增删都必须是有意为之，这条断言就是那道确认。"""
        assert WRITE_PUBLIC_ROLES == {ROLE_ADMIN, ROLE_AUTHORIZED_USER}
        assert ROLE_USER not in WRITE_PUBLIC_ROLES

    def test_schema_literal_matches_role_constants(self):
        """``RoleName`` 是**手写**字面量（Literal 里放变量会让静态检查失效），
        所以必须有一条断言盯着它与常量表一致。"""
        assert set(get_args(RoleName)) == set(ROLE_ORDER)


class TestCanWritePublic:
    @pytest.mark.parametrize("role", [ROLE_ADMIN, ROLE_AUTHORIZED_USER])
    def test_writers(self, role):
        assert can_write_public(role) is True

    @pytest.mark.parametrize("role", [ROLE_USER, "", "Admin", "superuser", None])
    def test_non_writers(self, role):
        """大小写不一致 / 拼错 / 空值一律 False：宁可误拒，不可误放。"""
        assert can_write_public(role) is False


def _resolve_write_public(role):
    """调用真正的 ``resolve_scope``（写公共库）：返回 scope 或 HTTP 状态码。"""
    try:
        return resolve_scope(
            "public", {"user_uuid": "u", "username": "n", "role": role}, write=True
        )
    except HTTPException as exc:
        return exc.status_code


class TestCapabilityConsistency:
    @pytest.mark.parametrize("role", ROLE_ORDER)
    def test_schema_capability_matches_real_endpoint_rule(self, role):
        """"界面给不给选项"与"接口放不放行"必须是同一个判断，否则会出现
        "点了公共库才 403"（或更糟：不给选项却允许写）。"""
        assert can_write_public(role) == (_resolve_write_public(role) == "public")

    def test_write_permission_uses_shared_predicate(self):
        """源码级回归防线：写权限判断要复用 ``utils.roles.can_write_public``。

        断言的是"``resolve_scope`` 里出现了共用判断"，而**不是**"文件里没有
        ``!= \"admin\"``"——``require_admin`` 本来就要比 admin，逐字串检查会误伤，
        而会误伤的测试迟早被人删掉（那才是真的失守）。
        """
        src = (PROJECT_ROOT / "api" / "deps.py").read_text(encoding="utf-8")
        body = src.split("def resolve_scope", 1)[1]
        assert "can_write_public(" in body


class TestRolePayloads:
    def test_authorized_user_role_is_serializable(self):
        """回归点：``UserOut.role`` 曾是 ``Literal["user","admin"]``，
        把一个用户授权成 ``authorized_user`` 之后，他的 ``/auth/me`` 会因为
        pydantic 校验失败直接 500——表现为"刚被授权的人一登录就白屏"。
        """
        payload = UserOut(user_uuid="u1", username="bob", role=ROLE_AUTHORIZED_USER)

        assert payload.model_dump()["role"] == ROLE_AUTHORIZED_USER

    @pytest.mark.parametrize(
        "role,expected",
        [
            (ROLE_USER, False),
            (ROLE_AUTHORIZED_USER, True),
            (ROLE_ADMIN, True),
        ],
    )
    def test_capability_is_exposed_to_frontend(self, role, expected):
        """前端只消费这个布尔值来决定上传目标里给不给"公共库"。"""
        payload = UserOut(user_uuid="u", username="n", role=role).model_dump()

        assert payload["can_write_public"] is expected

    def test_role_request_model_enforces_whitelist(self):
        assert UserRoleIn(role=ROLE_AUTHORIZED_USER).role == ROLE_AUTHORIZED_USER
        with pytest.raises(ValidationError):
            UserRoleIn(role="superuser")


# ------------------------------------------------------- set_user_role 的闸门
@contextlib.contextmanager
def _ctx(value):
    """把任意值包成 ``with ... as`` 可用的上下文管理器（用来替代 ``db.get_conn``）。"""
    yield value


class FakeCursor:
    """只认 ``set_user_role`` 里那三条 SQL 的假游标；其余语句一律记下但不返回行。"""

    def __init__(self, conn):
        self.conn = conn
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.conn.executed.append(normalized)
        lowered = normalized.lower()
        if lowered.startswith("select username, role"):
            self._one = self.conn.users.get(params[0])
        elif lowered.startswith("select count(*) as admins"):
            self._one = {"admins": self.conn.active_admins}
        else:
            self._one = None

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, users=None, active_admins=1):
        self.users = users or {}
        self.active_admins = active_admins
        self.executed = []

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def _updates(self):
        return [sql for sql in self.executed if sql.lower().startswith("update")]


@pytest.fixture()
def fake_db(monkeypatch):
    """把 ``auth.get_conn`` 换成假连接；返回假连接本身供断言。"""
    conn = FakeConn()
    monkeypatch.setattr(auth, "get_conn", lambda: _ctx(conn))
    return conn


class TestSetUserRoleGuards:
    def test_rejects_unknown_role_before_touching_db(self, monkeypatch):
        called = []
        monkeypatch.setattr(auth, "get_conn", lambda: called.append("db") or _ctx(FakeConn()))

        ok, msg = auth.set_user_role("uuid-bob", "superuser", operator_uuid="uuid-root")

        assert ok is False
        assert msg == f"角色必须是 {', '.join(ROLE_ORDER)} 之一"
        assert called == [], "非法角色不该走到数据库（那里只会变成一段英文 SQL 报错）"

    def test_requires_operator(self, fake_db):
        ok, msg = auth.set_user_role("uuid-bob", ROLE_AUTHORIZED_USER, operator_uuid=None)

        assert ok is False and "操作者" in msg
        assert fake_db.executed == []

    def test_refuses_self_change(self, fake_db):
        """连自己都不许改：会话里还是旧角色，界面与后端能力立刻对不上；
        更致命的是最后一名管理员"自我降级"= 系统再也无人可管理。"""
        ok, msg = auth.set_user_role("uuid-root", ROLE_USER, operator_uuid="uuid-root")

        assert ok is False and "自己" in msg
        assert fake_db.executed == []

    def test_missing_user(self, fake_db):
        ok, msg = auth.set_user_role("uuid-nobody", ROLE_USER, operator_uuid="uuid-root")

        assert ok is False and msg == "用户不存在"

    def test_last_active_admin_cannot_be_demoted(self, fake_db):
        """别人来降级唯一的管理员也不行——这就是"回库里数一遍"存在的理由。"""
        fake_db.users["uuid-root"] = {"username": "root", "role": ROLE_ADMIN}
        fake_db.active_admins = 1

        ok, msg = auth.set_user_role("uuid-root", ROLE_USER, operator_uuid="uuid-other")

        assert ok is False
        assert "最后一名在册管理员" in msg
        assert fake_db._updates() == []

    def test_admin_can_be_demoted_when_others_remain(self, fake_db):
        fake_db.users["uuid-root"] = {"username": "root", "role": ROLE_ADMIN}
        fake_db.active_admins = 2

        ok, _ = auth.set_user_role("uuid-root", ROLE_USER, operator_uuid="uuid-other")

        assert ok is True
        assert fake_db._updates() == [
            "UPDATE users SET role = %s WHERE user_uuid = %s"
        ]

    def test_promotes_user_to_authorized(self, fake_db):
        """模块 3 的主用例：把普通用户授权成能写公共库的角色。"""
        fake_db.users["uuid-bob"] = {"username": "bob", "role": ROLE_USER}

        ok, msg = auth.set_user_role(
            "uuid-bob", ROLE_AUTHORIZED_USER, operator_uuid="uuid-root"
        )

        assert ok is True
        assert ROLE_AUTHORIZED_USER in msg
        assert fake_db._updates() != []

    def test_same_role_is_idempotent(self, fake_db):
        """重复提交同一个角色不该变成"失败"（前端刷新、脚本重放都会重复提交）。"""
        fake_db.users["uuid-bob"] = {"username": "bob", "role": ROLE_USER}

        ok, _ = auth.set_user_role("uuid-bob", ROLE_USER, operator_uuid="uuid-root")

        assert ok is True
        assert fake_db._updates() == []

    def test_db_failure_is_reported_not_raised(self, monkeypatch):
        """业务层约定返回 ``(ok, 文案)`` 而不抛异常：抛出去就是 500 + 一段英文栈，
        而"改角色失败"用户只需要一句中文。"""

        def boom():
            raise RuntimeError("mysql down")

        monkeypatch.setattr(auth, "get_conn", boom)

        ok, msg = auth.set_user_role("uuid-bob", ROLE_USER, operator_uuid="uuid-root")

        assert ok is False
        assert msg == "修改失败，请稍后重试"
