"""``api.deps.resolve_scope`` 的授权矩阵（纯函数、离线）。

**这是 Phase 1 最值钱的一组测试。**

Streamlit 版把 scope 藏在 ``st.radio`` 的两个选项里，用户没有输入通道，
所以"只能访问 public / 自己的私人库"是天然成立的。换成 REST 之后
``scope`` 变成**客户端可任意构造的字符串**，只要漏一次校验：

    GET /api/files?scope=private:<别人的 user_uuid>
    GET /api/history?scope=private:<别人的 user_uuid>
    POST /api/chat/stream  {"scope": "private:<别人的 user_uuid>"}

就能直接读别人的私人库与对话历史。这类漏洞不会报错、不会告警，
只会安安静静地把全站数据交出去——所以必须有矩阵测试把它钉死。
"""
import pytest
from fastapi import HTTPException

from api.deps import PUBLIC_SCOPE, private_scope, resolve_scope

USER = {"user_uuid": "uuid-alice", "username": "alice", "role": "user"}
ADMIN = {"user_uuid": "uuid-root", "username": "root", "role": "admin"}


def _resolve(scope, user=USER, write=False):
    """返回可信 scope，越权时返回状态码（方便直接断言 403/400 的区别）。"""
    try:
        return resolve_scope(scope, user, write=write)
    except HTTPException as exc:
        return exc.status_code


class TestPublicScope:
    def test_user_can_read_public(self):
        assert _resolve("public", USER) == PUBLIC_SCOPE

    def test_admin_can_read_public(self):
        assert _resolve("public", ADMIN) == PUBLIC_SCOPE

    def test_user_cannot_write_public(self):
        """普通用户只能上传到私人库（app.py:104-108 的能力边界）。"""
        assert _resolve("public", USER, write=True) == 403

    def test_admin_can_write_public(self):
        assert _resolve("public", ADMIN, write=True) == PUBLIC_SCOPE


class TestPrivateScope:
    def test_user_can_read_own_private(self):
        assert _resolve("private:uuid-alice", USER) == "private:uuid-alice"

    def test_user_can_write_own_private(self):
        assert _resolve("private:uuid-alice", USER, write=True) == "private:uuid-alice"

    def test_user_cannot_read_other_private(self):
        assert _resolve("private:uuid-bob", USER) == 403

    def test_user_cannot_write_other_private(self):
        assert _resolve("private:uuid-bob", USER, write=True) == 403

    def test_admin_also_cannot_read_other_private(self):
        """隐私红线：管理员能写公共库，但**不能**翻他人私人库。"""
        assert _resolve("private:uuid-alice", ADMIN) == 403

    def test_suffix_trick_is_blocked(self):
        """``private:<自己>:<随便>`` 这类拼接后门必须被挡掉。"""
        assert _resolve("private:uuid-alice:extra", USER) == 403

    def test_returned_scope_is_server_authoritative(self):
        """返回的是用会话 uuid 重拼的 scope，而不是客户端原串。"""
        assert _resolve("  private:uuid-alice  ", USER) == private_scope(USER)


class TestInvalidScope:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            None,
            "PUBLIC",           # 大小写不一致：宁可 400，也不做"猜测式"宽容匹配
            "publicx",
            "PRIVATE:uuid-alice",
            "private",          # 少了前缀冒号
            "all",
            "../public",
        ],
    )
    def test_rejects_unknown_scope_with_400(self, bad):
        assert _resolve(bad, USER) == 400

    def test_empty_private_owner_is_denied(self):
        """``private:`` 空所有者不是合法 scope，也绝不能"退化"成 public。"""
        assert _resolve("private:", USER) in (400, 403)
