"""``api.deps.resolve_scope_set`` / ``scope_key`` 的授权矩阵（模块 4，纯函数、离线）。

单库矩阵在 ``test_scope_auth.py``；本文件锁的是模块 4 新增的"一次检索多个库"：

* ``both`` → ``[public, private:<自己>]``，且**顺序固定**——前端标签渲染顺序与
  缓存 key（``public+private:<uuid>``）都依赖它，顺序漂移会让"同一个模式下
  两次提问命中两份缓存"；
* ``both`` 只允许拼**自己**的私人库，任何情况下都不能把别人的拼进来；
* ``both`` 在写路径（上传 / 删除 / 列文件）必须被拒（400）：
  "同时传到两个库"没有答案，宁可直接拒绝也不要静默挑一个；
* 单库输入的返回值必须与 ``resolve_scope`` **逐字一致**：Redis 里的老缓存
  （``cache:<scope>:...``）与老历史（``chat_history:<user>:<scope>``）都是按单库
  key 存的，这里一旦多拼一个字符，升级后所有用户的老会话都会"看起来丢了"。
"""
import pytest
from fastapi import HTTPException

from api.deps import (
    PUBLIC_SCOPE,
    SCOPE_MODE_BOTH,
    describe_scope,
    private_scope,
    resolve_scope,
    resolve_scope_set,
    scope_key,
)

USER = {"user_uuid": "uuid-alice", "username": "alice", "role": "user"}
ADMIN = {"user_uuid": "uuid-root", "username": "root", "role": "admin"}


def _resolve(scope, user=USER, write=False):
    """返回可信 scope 列表，越权时返回状态码（便于直接断言 403 与 400 的区别）。"""
    try:
        return resolve_scope_set(scope, user, write=write)
    except HTTPException as exc:
        return exc.status_code


class TestBothMode:
    def test_both_is_public_plus_own_private(self):
        assert _resolve(SCOPE_MODE_BOTH, USER) == [PUBLIC_SCOPE, private_scope(USER)]

    def test_order_is_public_first(self):
        """顺序是契约：前端按它渲染「公共库 + 我的文档」，缓存 key 也按它拼。"""
        scopes = _resolve(SCOPE_MODE_BOTH, USER)

        assert scopes[0] == PUBLIC_SCOPE
        assert scopes[1].startswith("private:")

    def test_admin_gets_own_private(self):
        """管理员用 both 时拿到的也是**自己的**私人库，不是公共库的别名。"""
        assert _resolve(SCOPE_MODE_BOTH, ADMIN) == [PUBLIC_SCOPE, "private:uuid-root"]

    def test_private_part_comes_from_session_not_from_client(self):
        """私有库部分必须由会话 uuid 重拼：客户端根本没机会指定别人。"""
        scopes = _resolve(SCOPE_MODE_BOTH, USER)

        assert scopes[1] == private_scope(USER)
        assert "uuid-bob" not in scope_key(scopes)

    def test_whitespace_is_tolerated(self):
        assert _resolve("  both  ", USER) == [PUBLIC_SCOPE, private_scope(USER)]

    def test_both_rejected_on_write_path(self):
        """写路径（上传 / 删除）只接受具体库：both 必须 400 而不是"随便挑一个"。"""
        assert _resolve(SCOPE_MODE_BOTH, USER, write=True) == 400
        assert _resolve(SCOPE_MODE_BOTH, ADMIN, write=True) == 400


class TestSingleScopeIsUnchanged:
    @pytest.mark.parametrize("single", ["public", "private:uuid-alice", " private:uuid-alice "])
    def test_set_matches_single_scope(self, single, user=USER):
        """单库路径与 ``resolve_scope`` 必须完全等价（老缓存/老历史靠它不失效）。"""
        assert _resolve(single, user) == [resolve_scope(single, user)]

    def test_write_permission_still_enforced(self):
        """模块 3 的能力边界不能被模块 4 绕过：普通用户写公共库依然 403。"""
        assert _resolve("public", USER, write=True) == 403
        assert _resolve("public", ADMIN, write=True) == [PUBLIC_SCOPE]


class TestInvalidScope:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            None,
            "all",             # 老前端可能残留的写法：必须 400，不能当 both 处理
            "PUBLIC",          # 大小写不一致：宁可 400，也不做"猜测式"宽容匹配
            "bothx",
            "boths",
            "private:",        # 空所有者
            "private:uuid-bob",  # 别人的私人库
        ],
    )
    def test_rejected(self, bad):
        assert _resolve(bad) in (400, 403)

    def test_others_private_is_forbidden_not_bad_request(self):
        """越权（403）与非法（400）必须分开：403 是审计信号，400 只是客户端写错了。"""
        assert _resolve("private:uuid-bob") == 403

    def test_both_alone_cannot_bypass_other_private(self):
        """``both`` 是模式而不是"任意库"：它只等于 public + 自己，不含他人。"""
        assert "uuid-bob" not in " ".join(_resolve(SCOPE_MODE_BOTH, USER))


class TestScopeKey:
    def test_single_scope_key_is_the_scope_itself(self):
        assert scope_key([PUBLIC_SCOPE]) == PUBLIC_SCOPE
        assert scope_key(["private:uuid-alice"]) == "private:uuid-alice"

    def test_both_key_is_joined_in_order(self):
        assert scope_key([PUBLIC_SCOPE, "private:uuid-alice"]) == "public+private:uuid-alice"

    def test_different_users_do_not_share_cache_key(self):
        """both 的 key 带 uuid：两个用户不会命中同一份缓存（否则会串答案）。"""
        alice = scope_key(_resolve(SCOPE_MODE_BOTH, USER))
        root = scope_key(_resolve(SCOPE_MODE_BOTH, ADMIN))

        assert alice != root
        assert "uuid-alice" in alice and "uuid-root" in root


class TestLabels:
    def test_public_label(self):
        assert describe_scope(PUBLIC_SCOPE) == "公共库"

    def test_private_label_hides_uuid(self):
        """私人库的展示名不能把 uuid 甩到界面上。"""
        assert describe_scope("private:uuid-alice") == "我的文档"

    def test_both_label(self):
        assert describe_scope(SCOPE_MODE_BOTH) == "公共库 + 我的文档"
