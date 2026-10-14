"""对话历史路由：读取 / 清空（按 scope 隔离；模块 4 起支持 ``both``）。

历史存在 Redis 的 ``chat_history:<user_uuid>:<scope_key>`` 列表里（TTL 1 小时、
最多 10 轮，见 ``services/redis_service.py``），所以这里只是把 scope 校验好再转发。

⚠️ ``scope_key`` 的拼法必须与 ``api/routers/chat.py`` **逐字一致**（都用
``deps.scope_key``）：否则 both 模式下"问答写进 A 键、历史去读 B 键"，
用户看到的症状是"答完刷新历史就没了"。
"""
from typing import List

from fastapi import APIRouter, Depends

from api.deps import describe_scope, get_current_user, resolve_scope_set, scope_key
from api.schemas import HistoryMessage, MessageOut
from services import redis_service

router = APIRouter(prefix="/history", tags=["对话历史"])

_VALID_ROLES = ("user", "assistant")


@router.get("", response_model=List[HistoryMessage], summary="读取对话历史")
def read_history(
    scope: str = "public", user=Depends(get_current_user)
) -> List[HistoryMessage]:
    key = scope_key(resolve_scope_set(scope, user))
    rows = redis_service.get_history(user["user_uuid"], key)
    # 过滤掉角色的非预期值（历史格式若有变动，宁可少返回也不要 500）
    return [
        HistoryMessage(**row) for row in rows if row.get("role") in _VALID_ROLES
    ]


@router.delete("", response_model=MessageOut, summary="清空对话历史")
def clear_history(scope: str = "public", user=Depends(get_current_user)) -> MessageOut:
    scopes = resolve_scope_set(scope, user)
    redis_service.clear_history(user["user_uuid"], scope_key(scopes))
    # 文案用展示名而不是内部 key：both 的 key 是 "public+private:<uuid>"，
    # 直接丢给用户看既长又暴露 uuid。
    names = "、".join(describe_scope(s) for s in scopes)
    return MessageOut(message=f"已清空「{names}」的对话历史")
