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
from typing import Dict, List, Optional

from fastapi import Depends, Request

from config import settings
from utils.logger import logger
from utils.roles import can_write_public

from . import errors, sessions

PUBLIC_SCOPE = "public"
PRIVATE_PREFIX = "private:"

#: 模块 4 新增的检索范围模式：一次问答同时检公共库 + 自己的私人库。
#: 它**只在只读路径**（chat / history / feedback）成立：上传、删除、列文件都必须
#: 落到一个具体的库上（否则\"传到哪\"没有答案），所以那些路由继续拒收 it。
SCOPE_MODE_BOTH = "both"

#: 会话级 scope 模式（前端左侧栏的三个选项）
READ_SCOPE_MODES = (PUBLIC_SCOPE, SCOPE_MODE_BOTH)

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
    """把客户端传来的 scope 解析为**可信的单库 scope**，并做权限校验。

    规则（与 Streamlit 版的能力边界逐条对齐）：
    * ``public``            → 读：登录即可；写：``admin`` 或 ``authorized_user``
      （模块 3 的细粒度权限；普通用户只能传私人库，见 app.py:104-108）
    * ``private:<uuid>``    → 只允许 ``uuid == 当前用户``，否则 403
    * ``both``              → 400（模块 4：只读接口用 ``resolve_scope_set``；
      上传/删除/列文件必须落到具体库，否则"传到哪"没有答案）
    * 其它 / 空             → 400（不接受"默认放开"的写法，防止漏判变成越权）
    """
    return _resolve_single((scope or "").strip(), user, write)


def _resolve_single(raw: str, user: SessionUser, write: bool) -> str:
    if raw == PUBLIC_SCOPE:
        if write and not can_write_public(user.get("role", "")):
            # 文案里点明"可以找管理员授权"：模块 3 新增的 authorized_user 若不说出来
            # 就等于不存在，用户只会觉得"上传公共库"是个 bug。
            raise errors.forbidden(
                "只有管理员或已授权用户（authorized_user）可以写入公共库，"
                "如需上传到公共库请联系管理员授权"
            )
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

    if raw == SCOPE_MODE_BOTH:
        raise errors.bad_request(
            "scope 非法：both 只用于问答/历史等只读接口；"
            "上传、删除、列文件必须指定具体库（public 或 private:<user_uuid>）"
        )

    raise errors.bad_request(
        f"scope 非法：{raw!r}（只接受 public、private:<你的 user_uuid> 或 both）"
    )


#: 库的展示名（模块 4）：给"检索范围：公共库 + 我的文档"和错误文案共用一份文案，
#: 前端 ``client.ts`` 里有同名的 ``scopeLabel``，改动时两边一起改。
SCOPE_LABELS = {PUBLIC_SCOPE: "公共库", SCOPE_MODE_BOTH: "公共库 + 我的文档"}


def describe_scope(scope: str) -> str:
    """把 scope 变成人话（``private:<uuid>`` → 我的文档）。"""
    return SCOPE_LABELS.get(scope, "我的文档")


def scope_key(scopes: List[str]) -> str:
    """把若干可信 scope 压成一个字符串 key（缓存 / 历史 / 日志共用）。

    单库时**必须**原样返回 scope 本身：Redis 里的 ``cache:<scope>:<KB_VERSION>:...``
    与 ``chat_history:<user>:<scope>`` 都是既有关键字，模块 4 不能把它们作废
    （否则升级后所有用户的老会话/老缓存全部"看起来丢了"）。
    多库时拼成 ``public+private:<uuid>``：私有库带 uuid，天然按用户隔离，
    不存在"两个用户共用一份 both 缓存"的串答案风险。
    """
    return "+".join(scopes)


def resolve_scope_set(scope: Optional[str], user: SessionUser, write: bool = False) -> List[str]:
    """模块 4：把客户端 scope 解析为**可信 scope 列表**（一次问答可检索多个库）。

    * ``public`` / ``private:<自己>`` → 单元素列表（与 ``resolve_scope`` 逐字等价）
    * ``both`` → ``[public, private:<自己>]``，顺序即**权威顺序**：
      它决定前端标签渲染顺序与缓存 key（``public+private:<uuid>``），必须稳定。

    只读语义：``write=True`` 时 ``both`` 一律 400（见 ``_resolve_single`` 的说明）。
    """
    raw = (scope or "").strip()
    if raw == SCOPE_MODE_BOTH:
        if write:
            raise errors.bad_request(
                "scope 非法：both 不能用于写入（上传/删除必须指定具体库）"
            )
        # 公共库读权限：登录即可；私人库永远是自己的 → 两个都必然可读，无需再判角色。
        return [PUBLIC_SCOPE, private_scope(user)]
    return [_resolve_single(raw, user, write)]


def resolve_chat_scope(user: SessionUser) -> str:
    """问答默认库：普通用户=私人库，管理员=公共库（与 Streamlit 版默认选公共库一致）。"""
    return PUBLIC_SCOPE if user.get("role") == "admin" else private_scope(user)
