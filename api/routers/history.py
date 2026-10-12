"""对话历史路由：读取 / 清空（按 scope 隔离）。

历史存在 Redis 的 ``chat_history:<user_uuid>:<scope>`` 列表里（TTL 1 小时、
最多 10 轮，见 ``services/redis_service.py``），所以这里只是把 scope 校验好再转发。
"""
from typing import List

from fastapi import APIRouter, Depends

from api.deps import get_current_user, resolve_scope
from api.schemas import HistoryMessage, MessageOut
from services import redis_service

router = APIRouter(prefix="/history", tags=["对话历史"])

_VALID_ROLES = ("user", "assistant")


@router.get("", response_model=List[HistoryMessage], summary="读取对话历史")
def read_history(
    scope: str = "public", user=Depends(get_current_user)
) -> List[HistoryMessage]:
    resolved = resolve_scope(scope, user)
    rows = redis_service.get_history(user["user_uuid"], resolved)
    # 过滤掉角色的非预期值（历史格式若有变动，宁可少返回也不要 500）
    return [
        HistoryMessage(**row) for row in rows if row.get("role") in _VALID_ROLES
    ]


@router.delete("", response_model=MessageOut, summary="清空对话历史")
def clear_history(scope: str = "public", user=Depends(get_current_user)) -> MessageOut:
    resolved = resolve_scope(scope, user)
    redis_service.clear_history(user["user_uuid"], resolved)
    return MessageOut(message=f"已清空 {resolved} 的对话历史")
