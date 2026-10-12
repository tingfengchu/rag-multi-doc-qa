"""业务错误 → HTTP 响应的统一翻译层。

背景：业务层（``auth.create_user`` / ``auth.change_password`` / ``services.*``）约定
返回 ``(ok, 中文文案)`` 而**不抛异常**——这样 Streamlit 可以直接把文案丢给
``st.error()``。搬到 API 后必须把这套约定翻成状态码，否则要么丢文案、要么全变 500。

集中在这里做，避免每个路由各自编一套状态码/文案：
* 文案始终是可直接展示的中文（与 Streamlit 版逐字一致）；
* 响应体统一 ``{"detail": "..."}``（FastAPI 默认形状，前端一套解析逻辑）。
"""
from fastapi import HTTPException, status


class ApiError(HTTPException):
    """带中文文案的业务异常。"""


def bad_request(msg: str) -> ApiError:
    """参数/业务规则不满足（如"密码至少 8 位""当前密码不正确"）。"""
    return ApiError(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def unauthorized(msg: str = "未登录或会话已过期，请重新登录") -> ApiError:
    """没有会话 / 会话过期 / 凭证错误。"""
    return ApiError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=msg,
        headers={"WWW-Authenticate": "Cookie"},
    )


def forbidden(msg: str = "无权访问该资源") -> ApiError:
    """已登录但越权（访问他人私人库 / 非管理员访问管理端点）。"""
    return ApiError(status_code=status.HTTP_403_FORBIDDEN, detail=msg)


def not_found(msg: str = "资源不存在") -> ApiError:
    return ApiError(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


def conflict(msg: str) -> ApiError:
    """唯一性冲突（用户名已存在 / 文件已入库）。"""
    return ApiError(status_code=status.HTTP_409_CONFLICT, detail=msg)


def too_large(msg: str) -> ApiError:
    return ApiError(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=msg
    )


def ensure_ok(ok: bool, msg: str, code: int = status.HTTP_400_BAD_REQUEST) -> None:
    """把业务层的 ``(ok, msg)`` 直接转成 HTTP 异常：``ensure_ok(*change_password(...))``。

    这样路由里不会出现"检查漏了 ok 就继续跑"的写法——那类 bug 在 Streamlit 里
    只表现为一句错误提示，在 API 里就是"提示失败但数据已经改了"。
    """
    if not ok:
        raise ApiError(status_code=code, detail=msg)
