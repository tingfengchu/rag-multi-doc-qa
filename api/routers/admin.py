"""管理端路由：服务自检 / RAGAS 评测 / 报告下载。

对应 ``app.py:85-101``（依赖服务自检，"把沉默的降级明确暴露给管理员"）
与 ``app.py:427-498``（RAGAS 评测面板）。

全部挂 ``require_admin``：这些接口会暴露内部拓扑（ES/MySQL/Redis 版本与错误原文），
普通用户不该看到。
"""
import os

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from api import errors
from api.deps import require_admin
from api.schemas import HealthOut, RagasEvalIn, RagasEvalOut, ServiceStatus
from db import get_conn
from services import es_service, eval_service, redis_service, vector_service
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
