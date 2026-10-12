"""认证路由：注册 / 登录 / 登出 / 当前用户 / 改密码 / 会话信息。

路由里只有"翻译"工作，业务逻辑全在 ``auth.py`` 与 ``api/sessions.py``：
* ``(ok, 中文文案)`` → HTTP 状态码（``api/errors.py``）；
* token → HttpOnly Cookie（``api/sessions.py``）。
"""
from fastapi import APIRouter, Depends, Request, Response, status

from api import errors, sessions
from api.deps import get_current_user
from api.schemas import LoginIn, MessageOut, PasswordChangeIn, RegisterIn, UserOut
from auth import authenticate, change_password, create_user
from config import settings
from utils.logger import logger

router = APIRouter(prefix="/auth", tags=["认证"])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,          # JS 读不到 → XSS 偷不走 token
        samesite="lax",         # 挡掉跨站 POST 携带 Cookie（CSRF 的第一道防线）
        secure=settings.session_cookie_secure,  # 生产 HTTPS 必须 True
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="自助注册",
)
def register(payload: RegisterIn) -> UserOut:
    """自助注册（注册通道保持开放）。

    **角色固定为 ``user``，绝不接受客户端指定**：否则任何人 POST 一个
    ``{"role": "admin"}`` 就能把自己注册成管理员——``create_user`` 本身是支持
    role 参数的（脚本建管理员要用），所以这个约束必须在路由层显式做。
    """
    ok, result = create_user(payload.username, payload.password, role="user")
    if not ok:
        if "已存在" in result:
            raise errors.conflict(result)
        raise errors.bad_request(result)
    return UserOut(user_uuid=result, username=payload.username.strip(), role="user")


@router.post("/login", response_model=UserOut, summary="登录（写入 HttpOnly 会话 Cookie）")
def login(payload: LoginIn, response: Response) -> UserOut:
    info = authenticate(payload.username, payload.password)
    if not info:
        # 不区分"用户不存在"与"密码错误"，避免账号枚举
        logger.warning("login_failed", extra={"username": payload.username})
        raise errors.unauthorized("用户名或密码错误")

    try:
        token = sessions.create_session(info)
    except Exception as e:
        logger.error("session_create_failed", extra={"err": str(e)})
        raise errors.ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="会话服务暂不可用，请稍后重试",
        ) from e

    _set_session_cookie(response, token)
    logger.info("login_ok", extra={"user_uuid": info["user_uuid"]})
    return UserOut(**info)


@router.post("/logout", response_model=MessageOut, summary="登出（吊销当前会话）")
def logout(request: Request, response: Response) -> MessageOut:
    """登出：吊销 Redis 里的会话并清 Cookie。

    不要求"必须已登录"——重复登出、Cookie 已过期的情况都应该安静成功，
    否则前端会在登出时弹一个毫无意义的 401。
    """
    token = request.cookies.get(settings.session_cookie_name)
    sessions.revoke_session(token)
    _clear_session_cookie(response)
    return MessageOut(message="已退出登录")


@router.get("/me", response_model=UserOut, summary="当前登录用户")
def me(user=Depends(get_current_user)) -> UserOut:
    return UserOut(**user)


@router.get("/session", response_model=MessageOut, summary="会话存活探测")
def session_probe(user=Depends(get_current_user)) -> MessageOut:
    """前端启动时探测会话是否还有效（比解析 401 更省事）。"""
    return MessageOut(message=f"{user['username']} · {user['role']}")


@router.post(
    "/password",
    response_model=MessageOut,
    summary="修改自己的密码（成功后吊销全部会话）",
)
def change_own_password(
    payload: PasswordChangeIn,
    response: Response,
    user=Depends(get_current_user),
) -> MessageOut:
    """改密码：旧密码必填 → 强度校验 → 落库 → **吊销该用户全部会话**。

    最后一步是本设计与 Streamlit 版的差异点，也是必须的：Cookie 里的 token
    可能早已被复制到别处（共享电脑、被抓包），改完密码若不吊销，
    攻击者手里的旧 token 依然有效——"改了个寂寞"。
    """
    ok, msg = change_password(
        user["user_uuid"],
        payload.old_password,
        payload.new_password,
        payload.confirm_password,
    )
    if not ok:
        raise errors.bad_request(msg)

    revoked = sessions.revoke_all_sessions(user["user_uuid"])
    _clear_session_cookie(response)
    logger.info(
        "password_changed_via_api",
        extra={"user_uuid": user["user_uuid"], "revoked": revoked},
    )
    return MessageOut(
        message=f"密码修改成功，已退出全部 {revoked} 个会话，请用新密码重新登录"
    )
