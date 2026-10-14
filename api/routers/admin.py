"""管理端路由：服务自检 / RAGAS 评测 / 报告下载 / 账号角色授权。

对应 ``app.py:85-101``（依赖服务自检，"把沉默的降级明确暴露给管理员"）
与 ``app.py:427-498``（RAGAS 评测面板）；``/users*`` 两个端点是模块 3 新增的
细粒度权限控制（把普通用户授权成 ``authorized_user``，允许其写公共库）。

全部挂 ``require_admin``：这些接口会暴露内部拓扑（ES/MySQL/Redis 版本与错误原文），
普通用户不该看到；账号列表更是直接列出全站用户。
"""
import os
from typing import List

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from api import errors, sessions
from api.deps import require_admin
from api.schemas import (
    AdminStatsOut,
    AdminUserOut,
    HealthOut,
    RagasEvalIn,
    RagasEvalOut,
    ServiceStatus,
    UserRoleIn,
)
from auth import get_user, list_users, set_user_role
from db import get_conn
from services import es_service, eval_service, redis_service, stats_service, vector_service
from services.es_service import es
from utils.logger import logger

router = APIRouter(prefix="/admin", tags=["管理端"])


def _check_mysql() -> ServiceStatus:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return ServiceStatus(ok=True, detail="ok")
    except Exception as e:
        return ServiceStatus(ok=False, detail=str(e))


def _check_redis() -> ServiceStatus:
    try:
        redis_service.r.ping()
        return ServiceStatus(ok=True, detail="ok")
    except Exception as e:
        return ServiceStatus(ok=False, detail=str(e))


def _check_es() -> ServiceStatus:
    try:
        info = es.info()
        version = (info.get("version") or {}).get("number", "?")
        return ServiceStatus(ok=True, detail=f"version={version}")
    except Exception as e:
        return ServiceStatus(ok=False, detail=str(e))


@router.get("/health", response_model=HealthOut, summary="依赖服务自检（管理员）")
def admin_health(user=Depends(require_admin)) -> HealthOut:
    """一次性汇报 ES / MySQL / Redis 与公共库向量块数。

    为什么要这个接口：以前依赖挂掉时应用是**静默降级**（关键词检索不可用但页面照常），
    只有管理员在调试面板里才发现。REST 化之后把它做成可探针调用的接口。
    """
    es_status = _check_es()
    try:
        uses_ik = bool(es_service.ik_available())
    except Exception:
        uses_ik = False

    try:
        blocks = vector_service.count(vector_service.get_vectorstore("public"))
    except Exception as e:
        logger.warning("chroma_count_failed", extra={"err": str(e)})
        blocks = 0

    return HealthOut(
        es=es_status,
        es_uses_ik=uses_ik,
        mysql=_check_mysql(),
        redis=_check_redis(),
        chroma_blocks=blocks,
    )


# --------------------------- 账号与授权（模块 3） ---------------------------
@router.get(
    "/users",
    response_model=List[AdminUserOut],
    summary="账号列表（管理员，授权面板用）",
)
def admin_list_users(user=Depends(require_admin)) -> List[AdminUserOut]:
    """列出全部账号，供管理员在授权面板里选人。

    模块 3 顺带加这一个只读接口的理由：``PUT .../role`` 需要 ``user_uuid``，
    没有列表的话管理员只能"去数据库里手抄 uuid 再调接口"——那等于这个功能没落地。
    """
    try:
        rows = list_users()
    except Exception as e:
        logger.error("admin_list_users_failed", extra={"err": str(e)})
        raise errors.ApiError(
            status_code=500, detail="读取账号列表失败，请稍后重试"
        ) from e
    return [AdminUserOut(**row) for row in rows]


@router.get(
    "/stats",
    response_model=AdminStatsOut,
    summary="运营看板统计（管理员）：高频提问 / 高频文档 / 无结果问题 / 反馈汇总",
)
def admin_stats(
    window: int = Query(
        stats_service.STATS_WINDOW,
        ge=50,
        le=100_000,
        description="统计最近多少次检索（默认 5000；越大越慢，看板看的是最近的运营状况）",
    ),
    user=Depends(require_admin),
) -> AdminStatsOut:
    """看板的四个块（模块 6）。

    聚合放在 ``services/stats_service``：那里负责"怎么算"（窗口、排序、不 JOIN），
    这里只负责"谁能看"。``ge=50`` 拦掉 ``?window=1`` 这类"只看一条流水"的调用——
    那种数字看着像统计，实际是一个样本，比没数据更容易误导人。

    失败回 500 而不是空数据：看板上"四个块全是 0"与"统计读不出来"必须能分开，
    否则数据库一挂，管理员看到的是"系统今天没人用"。
    """
    try:
        data = stats_service.build_stats(window=window)
    except Exception as e:
        logger.error("admin_stats_failed", extra={"err": str(e), "window": window})
        raise errors.ApiError(status_code=500, detail="统计读取失败，请稍后重试") from e

    logger.info("admin_stats_ok", extra={"window": window})
    return AdminStatsOut(**data)


@router.put(
    "/users/{user_uuid}/role",
    response_model=AdminUserOut,
    summary="修改他人角色（管理员）——授权 user 写公共库",
)
def admin_set_user_role(
    user_uuid: str, payload: UserRoleIn, user=Depends(require_admin)
) -> AdminUserOut:
    """把某个账号设为 ``user`` / ``authorized_user`` / ``admin``。

    两个必须点明的行为：

    * **改完立刻吊销该用户全部会话**。会话里存的是**登录那一刻**的角色，不吊销的话
      "降级"要等对方下次登录才生效——中间那段时间他手上仍有旧权限，
      这类"改了但没生效"的降权最容易被误认为已经修好的漏洞；
    * 返回**目标用户**的最终状态而不是一句 message：前端拿到就能直接刷新那一行。
    """
    ok, msg = set_user_role(user_uuid, payload.role, operator_uuid=user["user_uuid"])
    if not ok:
        # 业务拒绝分两类：人找不到 → 404；规则不允许（改自己 / 最后一名管理员）→ 400
        if msg == "用户不存在":
            raise errors.not_found(msg)
        raise errors.bad_request(msg)

    revoked = sessions.revoke_all_sessions(user_uuid)
    logger.info(
        "user_role_changed_via_api",
        extra={
            "target": user_uuid,
            "role": payload.role,
            "operator": user["user_uuid"],
            "revoked_sessions": revoked,
        },
    )

    row = get_user(user_uuid)
    if not row:  # 极小概率：刚改完账号就被删了
        raise errors.not_found("用户不存在")
    return AdminUserOut(**row)


@router.post(
    "/eval/ragas",
    response_model=RagasEvalOut,
    summary="跑一遍 RAGAS 检索质量评测（管理员，消耗 LLM 额度）",
)
def run_ragas(payload: RagasEvalIn, user=Depends(require_admin)) -> RagasEvalOut:
    """对黄金测试集跑真实链路 + RAGAS 打分。

    RAGAS 装不上是**预期情况**（它会把 openai 3.x 降级成 1.x，所以只装在
    ``.venv-eval``），因此这里返回可展示的中文指引而不是 500。
    """
    if not eval_service.ragas_available():
        raise errors.bad_request(
            "当前环境未装 RAGAS。评测依赖请装到独立的 .venv-eval（主环境刻意不装，"
            "因为 ragas 会把 openai 3.x 降级成 1.x，影响生成链路的 SDK 版本）。"
            f"当前 ragas 版本：{eval_service.ragas_version()}"
        )

    try:
        report = eval_service.run_ragas_evaluation(
            scope=payload.scope, limit=payload.limit
        )
    except Exception as e:
        logger.error("ragas_eval_failed", extra={"err": str(e)})
        raise errors.ApiError(status_code=500, detail=f"评测失败：{e}") from e

    logger.info(
        "ragas_eval_done",
        extra={"scope": payload.scope, "limit": payload.limit},
    )
    return RagasEvalOut(**report)


@router.get("/eval/report", summary="下载 RAGAS 报告 JSON（管理员）")
def download_report(
    path: str = Query(..., description="评测接口返回的 report_path"),
    user=Depends(require_admin),
):
    """按路径下载报告文件。

    ⚠️ **路径穿越防护**：只允许 ``evaluation_output`` 目录下的文件。
    没有这道校验，``?path=C:\\Windows\\win.ini`` 就是一个任意文件读取漏洞
    （而且它发生在"管理员"接口里，最容易被忽略）。
    """
    root = os.path.abspath(eval_service.DEFAULT_OUTPUT_DIR)
    target = os.path.abspath(path)
    if not target.startswith(root + os.sep) or not os.path.isfile(target):
        raise errors.bad_request("报告路径非法或文件不存在")

    return FileResponse(
        target,
        media_type="application/json",
        filename=os.path.basename(target),
    )
