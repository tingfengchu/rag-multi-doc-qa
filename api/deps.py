"""依赖注入：会话鉴权 + **scope 授权**。

``resolve_scope`` 是本次迁移最重要的安全收口点：

Streamlit 版把 scope 藏在 ``st.radio`` 的两个选项里，用户没有输入通道，
所以"能不能访问 public / private:<自己>"天生成立。换成 REST 之后，``scope``
变成了**客户端可任意构造的字符串**——只要不做校验：

    GET /api/files?scope=private:<别人的 user_uuid>

就能直接列别人的私人库、读别人的对话历史。这个洞不会报错、不会告警，
只会安安静静地把全站私人库暴露出去，所以校验必须集中在**所有**路由共用的
一个函数里，并且必须有单测矩阵覆盖（见 ``tests/unit/test_scope_auth.py``）。
"""
from typing import Dict, Optional

from fastapi import Depends, Request

from config import settings
from utils.logger import logger

from . import errors, sessions

PUBLIC_SCOPE = "public"
PRIVATE_PREFIX = "private:"

#: 会话里携带的用户信息（不含密码哈希，任何时刻都不落 Cookie 明文）
SessionUser = Dict[str, str]


def get_current_user(request: Request) -> SessionUser:
    """从 HttpOnly Cookie 里取会话；没有/过期一律 401。"""
    token = request.cookies.get(settings.session_cookie_name)
    user = sessions.get_session(token)
    if not user:
        raise errors.unauthorized()
    return user


def require_admin(user: SessionUser = Depends(get_current_user)) -> SessionUser:
    """管理员专属端点。普通用户访问返回 403（不是 401——身份是好的，只是没权限）。"""
    if user.get("role") != "admin":
        logger.warning(
            "admin_access_denied",
            extra={"user_uuid": user.get("user_uuid"), "path": "admin"},
        )
        raise errors.forbidden("需要管理员权限")
    return user


def private_scope(user: SessionUser) -> str:
    """当前用户的私人库 scope（服务端权威拼装，永远用会话里的 uuid）。"""
    return f"{PRIVATE_PREFIX}{user['user_uuid']}"


def resolve_scope(scope: Optional[str], user: SessionUser, write: bool = False) -> str:
    """把客户端传来的 scope 解析为**可信** scope，并做权限校验。

    规则（与 Streamlit 版的能力边界逐条对齐）：
    * ``public``            → 读：登录即可；写：仅 admin（普通用户只能传私人库，见 app.py:106-108）
    * ``private:<uuid>``    → 只允许 ``uuid == 当前用户``，否则 403
    * 其它 / 空             → 400（不接受"默认放开"的写法，防止漏判变成越权）
    """
    raw = (scope or "").strip()
    if raw == PUBLIC_SCOPE:
        if write and user.get("role") != "admin":
            raise errors.forbidden("只有管理员可以写入公共库")
        return PUBLIC_SCOPE

    if raw.startswith(PRIVATE_PREFIX):
        owner = raw[len(PRIVATE_PREFIX):]
        if owner != user.get("user_uuid"):
            logger.warning(
                "scope_access_denied",
                extra={"user_uuid": user.get("user_uuid"), "requested_owner": owner},
            )
            raise errors.forbidden("无权访问他人的私人库")
        # 用会话里的 uuid 重新拼装，彻底杜绝 "private:<uuid>:xxx" 之类的拼接后门
        return private_scope(user)

    raise errors.bad_request(
        f"scope 非法：{raw!r}（只接受 public 或 private:<你的 user_uuid>）"
    )


def resolve_chat_scope(user: SessionUser) -> str:
    """问答默认库：普通用户=私人库，管理员=公共库（与 Streamlit 版默认选公共库一致）。"""
    return PUBLIC_SCOPE if user.get("role") == "admin" else private_scope(user)
