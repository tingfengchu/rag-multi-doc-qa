"""改密码链路的集成测试（需要 MySQL 已启动，不需要 ES / Redis / LLM）。

**不碰真实数据**：每个用例创建一个 ``pwd_test_<随机>`` 临时账号，跑完立刻物理删除，
所以不会污染 ``users`` 表里真实账号，失败时也不会留下垃圾记录。

运行：``.venv\\Scripts\\python.exe -m pytest -m integration -v``
"""
import uuid
import warnings

import pytest

from auth import authenticate, change_password, create_user, verify_password
from db import get_conn

OLD_PASSWORD = "OldPass_123!"
NEW_PASSWORD = "NewPass_456!"


@pytest.fixture()
def temp_user():
    """建一个临时账号，yield 出 ``(user_uuid, username)``，用完物理删除。"""
    username = f"pwd_test_{uuid.uuid4().hex[:12]}"
    ok, result = create_user(username, OLD_PASSWORD, role="user")
    assert ok, f"创建临时账号失败：{result}"
    user_uuid = result

    try:
        yield user_uuid, username
    finally:
        # 清理失败只提示，不能吞掉真正的断言失败
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM users WHERE user_uuid = %s", (user_uuid,))
        except Exception as e:  # pragma: no cover
            # 不用 print + emoji：GBK 控制台（cp936）下 emoji 会抛 UnicodeEncodeError，
            # 掩盖掉真正的断言失败；这条信息属于告警，走 warnings 更合适。
            warnings.warn(f"清理临时账号失败（请手动删除 {username}）：{e}", stacklevel=2)


def _stored_hash(user_uuid: str):
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT password_hash FROM users WHERE user_uuid = %s", (user_uuid,)
            )
            row = cur.fetchone()
    return row["password_hash"] if row else None


@pytest.mark.integration
def test_change_password_updates_db_and_invalidates_old_password(temp_user):
    """成功路径：哈希换新 + 新密码可登录 + 旧密码立即失效。"""
    user_uuid, username = temp_user
    before = _stored_hash(user_uuid)

    ok, msg = change_password(user_uuid, OLD_PASSWORD, NEW_PASSWORD, NEW_PASSWORD)
    assert ok, msg

    after = _stored_hash(user_uuid)
    assert after != before, "哈希没有落库"
    assert verify_password(NEW_PASSWORD, after) is True
    assert authenticate(username, NEW_PASSWORD) is not None, "新密码无法登录"
    assert authenticate(username, OLD_PASSWORD) is None, "旧密码仍然可用，安全隐患"


@pytest.mark.integration
def test_wrong_old_password_rejected(temp_user):
    user_uuid, username = temp_user
    ok, msg = change_password(user_uuid, "WrongOld_999!", NEW_PASSWORD, NEW_PASSWORD)
    assert ok is False
    assert msg == "当前密码不正确"
    assert authenticate(username, OLD_PASSWORD) is not None, "被拒后原密码不该失效"


@pytest.mark.integration
@pytest.mark.parametrize(
    "old, new, confirm, expected",
    [
        (OLD_PASSWORD, OLD_PASSWORD, OLD_PASSWORD, "新密码不能与当前密码相同"),
        (OLD_PASSWORD, "short", "short", "密码至少 8 位"),
        (OLD_PASSWORD, NEW_PASSWORD, "NewPass_456", "两次输入的新密码不一致"),
        ("", NEW_PASSWORD, NEW_PASSWORD, "请输入当前密码"),
    ],
)
def test_rejected_changes_leave_password_intact(temp_user, old, new, confirm, expected):
    """所有被拒绝的路径都不许写库（否则会出现"提示失败但密码已经变了"）。"""
    user_uuid, username = temp_user
    ok, msg = change_password(user_uuid, old, new, confirm)
    assert ok is False
    assert msg == expected
    assert authenticate(username, OLD_PASSWORD) is not None
    assert authenticate(username, NEW_PASSWORD) is None


@pytest.mark.integration
def test_unknown_user_rejected():
    """不存在的 user_uuid：返回可展示文案而不是抛异常（避免 500）。"""
    ok, msg = change_password(str(uuid.uuid4()), OLD_PASSWORD, NEW_PASSWORD, NEW_PASSWORD)
    assert ok is False
    assert msg == "账号不存在，请重新登录"
