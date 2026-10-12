"""``api.sessions`` 的单元测试（纯离线：用内存假 Redis，不启动任何服务）。

这里锁定的契约：

1. 签发/读取往返一致，token **不透明**（不含用户名/uuid，泄露也无法反推身份）；
2. 每次登录 token 都不同（不能靠"猜一个固定值"复用别人的会话）；
3. 未知 / 已删除 / 已过期 / 数据损坏的 token 一律返回 ``None`` 而**不抛异常**
   （未捕获异常在 Web 层就是 500，且可能把 Redis 细节暴露给前端）；
4. **Redis 不可用时"失败即拒绝"**（fail closed）：宁可让用户重新登录，
   也绝不能因为读不到会话就放行；
5. 吊销的隔离性：吊销 A 的全部会话**不许**波及 B——这是多租户系统的红线。
"""
import json
from typing import Dict, Set

import pytest

from api import sessions

USER_A = {"user_uuid": "uuid-a", "username": "alice", "role": "user"}
USER_B = {"user_uuid": "uuid-b", "username": "bob", "role": "admin"}


class FakeRedis:
    """只实现 ``api/sessions.py`` 用到的那几个命令，避免为一个测试引入 fakeredis 依赖。"""

    def __init__(self):
        self.strings: Dict[str, str] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.expires: Dict[str, int] = {}

    # ---- string ----
    # 用 ``set(..., ex=)`` 而不是已弃用的 ``setex``（与 api/sessions.py 保持一致）
    def set(self, key, value, ex=None, **kwargs):
        self.strings[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    def get(self, key):
        return self.strings.get(key)

    def delete(self, *keys):
        removed = 0
        for key in keys:
            hit = False
            if key in self.strings:
                del self.strings[key]
                hit = True
            if key in self.sets:
                del self.sets[key]
                hit = True
            self.expires.pop(key, None)
            removed += 1 if hit else 0
        return removed

    # ---- set ----
    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def srem(self, key, *values):
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        hit = sum(1 for v in values if v in bucket)
        bucket.difference_update(values)
        return hit

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, ttl):
        self.expires[key] = ttl
        return True


class BrokenRedis(FakeRedis):
    """模拟 Redis 抖动：读操作直接抛异常。"""

    def get(self, key):
        raise ConnectionError("redis is down")


@pytest.fixture()
def fake():
    return FakeRedis()


class TestIssueAndRead:
    def test_roundtrip(self, fake):
        token = sessions.create_session(USER_A, client=fake)

        payload = sessions.get_session(token, client=fake)

        assert payload == USER_A
        assert fake.expires[sessions._session_key(token)] == 8 * 3600

    def test_token_is_opaque(self, fake):
        token = sessions.create_session(USER_A, client=fake)

        assert "alice" not in token
        assert "uuid-a" not in token

    def test_each_login_gets_a_new_token(self, fake):
        first = sessions.create_session(USER_A, client=fake)
        second = sessions.create_session(USER_A, client=fake)

        assert first != second
        assert sessions.get_session(first, client=fake) == USER_A
        assert sessions.get_session(second, client=fake) == USER_A

    def test_custom_ttl_is_applied(self, fake):
        token = sessions.create_session(USER_A, client=fake, ttl=60)

        assert fake.expires[sessions._session_key(token)] == 60


class TestInvalidInput:
    @pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
    def test_unknown_or_empty_token_returns_none(self, fake, token):
        assert sessions.get_session(token, client=fake) is None

    def test_revoked_session_returns_none(self, fake):
        token = sessions.create_session(USER_A, client=fake)

        assert sessions.revoke_session(token, client=fake) is True
        assert sessions.get_session(token, client=fake) is None

    def test_corrupted_payload_returns_none(self, fake):
        token = sessions.create_session(USER_A, client=fake)
        fake.strings[sessions._session_key(token)] = "{not json"

        assert sessions.get_session(token, client=fake) is None

    def test_payload_without_user_uuid_returns_none(self, fake):
        token = sessions.create_session(USER_A, client=fake)
        fake.strings[sessions._session_key(token)] = json.dumps({"username": "alice"})

        assert sessions.get_session(token, client=fake) is None

    def test_redis_failure_fails_closed(self):
        assert sessions.get_session("whatever", client=BrokenRedis()) is None


class TestRevocation:
    def test_revoke_session_removes_token_from_user_set(self, fake):
        token = sessions.create_session(USER_A, client=fake)

        sessions.revoke_session(token, client=fake)

        assert sessions.list_session_tokens("uuid-a", client=fake) == []

    def test_revoke_all_revokes_every_token_of_that_user(self, fake):
        t1 = sessions.create_session(USER_A, client=fake)
        t2 = sessions.create_session(USER_A, client=fake)

        assert sessions.revoke_all_sessions("uuid-a", client=fake) == 2
        assert sessions.get_session(t1, client=fake) is None
        assert sessions.get_session(t2, client=fake) is None

    def test_revoke_all_does_not_touch_other_users(self, fake):
        a_token = sessions.create_session(USER_A, client=fake)
        b_token = sessions.create_session(USER_B, client=fake)

        sessions.revoke_all_sessions("uuid-a", client=fake)

        assert sessions.get_session(a_token, client=fake) is None
        assert sessions.get_session(b_token, client=fake) == USER_B

    def test_revoke_all_with_no_sessions_returns_zero(self, fake):
        assert sessions.revoke_all_sessions("uuid-nobody", client=fake) == 0
