"""会话存储：Redis 里的**不透明 token** + HttpOnly Cookie。

为什么不用 JWT（Phase 1 最关键的取舍）：
    JWT 是自证明的，服务端无法单方面作废——**改了密码，被窃取的旧 token 依然能用到过期**。
    本系统刚做完"自助改密码"，如果改完密码旧会话还活着，那个功能就等于半个摆设。
    不透明 token 存 Redis 可以**立刻吊销**（改密码 → 吊销该用户全部会话）。
    代价是每请求多一次 Redis GET（本地亚毫秒级），换来"可吊销"，且无需新依赖（redis 已在）。

为什么放 Cookie 而不是 localStorage：
    HttpOnly Cookie 前端 JS 读不到，XSS 偷不走 token；``SameSite=Lax`` 顺带挡掉跨站 POST 的 CSRF。

可测试性：**所有函数都接受可选 ``client``**，单测可以直接塞一个内存假 Redis，
不必启动任何服务（见 ``tests/unit/test_sessions.py``）。
"""
import json
import secrets
from typing import Dict, List, Optional

from config import settings
from utils.logger import logger

SESSION_PREFIX = "session:"
USER_SESSIONS_PREFIX = "user_sessions:"

_client = None


def get_client():
    """懒加载 Redis 客户端（连接参数与 Streamlit 版一致，读同一份 config）。"""
    global _client
    if _client is None:
        import redis

        _client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
    return _client


def new_token() -> str:
    """32 字节 URL-safe 随机串（约 256 bit 熵，不可枚举、不含任何用户信息）。"""
    return secrets.token_urlsafe(32)


def _session_key(token: str) -> str:
    return f"{SESSION_PREFIX}{token}"


def _user_key(user_uuid: str) -> str:
    return f"{USER_SESSIONS_PREFIX}{user_uuid}"


def create_session(user: Dict, client=None, ttl: Optional[int] = None) -> str:
    """登录成功：签发 token 并写入 Redis，返回 token。

    同时维护 ``user_sessions:<uuid>`` 集合，才能在"改密码/登出全部"时一次性吊销。
    """
    client = client or get_client()
    ttl = settings.session_ttl_seconds if ttl is None else ttl
    token = new_token()

    payload = {
        "user_uuid": user["user_uuid"],
        "username": user["username"],
        "role": user["role"],
    }
    client.set(_session_key(token), json.dumps(payload, ensure_ascii=False), ex=ttl)
    client.sadd(_user_key(user["user_uuid"]), token)
    client.expire(_user_key(user["user_uuid"]), ttl)
    logger.info("session_created", extra={"user_uuid": user["user_uuid"]})
    return token


def get_session(token: Optional[str], client=None) -> Optional[Dict]:
    """按 token 取会话；不存在/已过期/数据损坏/Redis 不可用都返回 ``None``。

    注意这里是**失败即拒绝**（fail closed）：Redis 抖动时宁可让用户重新登录，
    也不能因为"读不到会话"就放行。
    """
    if not token:
        return None
    client = client or get_client()
    try:
        raw = client.get(_session_key(token))
    except Exception as e:
        logger.error("session_redis_error", extra={"err": str(e)})
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("session_payload_corrupted", extra={"token_prefix": token[:6]})
        return None
    if not isinstance(data, dict) or not data.get("user_uuid"):
        return None
    return data


def revoke_session(token: Optional[str], client=None) -> bool:
    """登出：删除单个会话（同时从用户会话集合里摘掉，避免集合无限膨胀）。"""
    if not token:
        return False
    client = client or get_client()
    payload = get_session(token, client=client)
    if payload:
        try:
            client.srem(_user_key(payload["user_uuid"]), token)
        except Exception as e:  # 摘不掉不影响登出本身
            logger.warning("session_srem_failed", extra={"err": str(e)})
    deleted = client.delete(_session_key(token))
    return bool(deleted)


def revoke_all_sessions(user_uuid: str, client=None) -> int:
    """吊销某用户的**全部**会话，返回吊销数量。

    改密码后必调：否则旧 Cookie（可能已被窃取）还能继续访问，等于密码白改。
    """
    client = client or get_client()
    try:
        tokens = client.smembers(_user_key(user_uuid)) or []
    except Exception as e:
        logger.error("session_smembers_failed", extra={"err": str(e)})
        return 0
    tokens = list(tokens)
    if not tokens:
        return 0
    client.delete(*[_session_key(t) for t in tokens])
    client.delete(_user_key(user_uuid))
    logger.info(
        "sessions_revoked_all", extra={"user_uuid": user_uuid, "count": len(tokens)}
    )
    return len(tokens)


def list_session_tokens(user_uuid: str, client=None) -> List[str]:
    """列出某用户当前会话 token（运维排查用）。"""
    client = client or get_client()
    return list(client.smembers(_user_key(user_uuid)) or [])
