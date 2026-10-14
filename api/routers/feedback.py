"""反馈路由：提交评价（👍/👎+原因）与 bad case 查询（管理员）。

对应 ``app.py:382-424`` 的反馈飞轮；``bad-cases`` 是 API 新增的能力
（Streamlit 版没有这个入口，但反馈表一直在写数据，一直没人看）。
"""
from typing import List

from fastapi import APIRouter, Depends, Query

from api import errors
from api.deps import (
    SCOPE_MODE_BOTH,
    get_current_user,
    require_admin,
    resolve_scope_set,
)
from api.schemas import BadCaseOut, FeedbackIn, FeedbackOut
from services import feedback_service
from utils.logger import logger

router = APIRouter(prefix="/feedback", tags=["反馈"])


@router.post("", response_model=FeedbackOut, summary="提交一条问答反馈")
def submit_feedback(
    payload: FeedbackIn, user=Depends(get_current_user)
) -> FeedbackOut:
    # 不信任 body 里的 scope：越权构造 scope 只会污染别人的统计口径
    scopes = resolve_scope_set(payload.scope, user)
    # 模块 4：多库检索（both）的反馈也要收得下，否则 both 模式下点👍/👎 会 400。
    # 落库写的是 `both` **字面量**而不是 "public+private:<uuid>"：feedback 表是统计口径，
    # 把 uuid 塞进去既没有检索价值，还会让"同一个模式"的样本在统计里被拆成每用户一份。
    resolved = SCOPE_MODE_BOTH if len(scopes) > 1 else scopes[0]

    # q_hash 由**服务端**算（模块 6）：前端拿它做「已反馈」的幂等键。
    # 先查一次是为了告诉前端"这次是新建还是更新"——两种情况对用户是同一件事
    # （"已反馈"），但对运营是不同的信号（前者是新样本，后者只是改了评价）。
    q_hash = feedback_service.question_hash(payload.question)
    updated = feedback_service.find_by_hash(user["user_uuid"], q_hash) is not None

    ok = feedback_service.save_feedback(
        user["user_uuid"],
        resolved,
        payload.question,
        payload.rewritten_question,
        payload.answer,
        [c.model_dump() for c in payload.citations],
        payload.rating,
        reason=payload.reason,
        file_uuid_filter=payload.file_uuid_filter,
    )
    if not ok:
        # save_feedback 内部已记日志，这里只回一句可展示的文案（不暴露 SQL 细节）
        raise errors.ApiError(status_code=500, detail="反馈保存失败，请稍后重试")

    logger.info(
        "feedback_api_ok",
        extra={"rating": payload.rating, "scope": resolved, "updated": updated},
    )
    message = "已更新您的反馈，感谢补充！" if updated else "感谢您的反馈，我们会持续优化！"
    return FeedbackOut(message=message, q_hash=q_hash, updated=updated)


@router.get("/bad-cases", response_model=List[BadCaseOut], summary="最近的差评样本（管理员）")
def list_bad_cases(
    limit: int = Query(50, ge=1, le=500),
    user=Depends(require_admin),
) -> List[BadCaseOut]:
    """按时间倒序返回 👎 的样本，供运营定位"答得不好"的问题。"""
    return [BadCaseOut(**row) for row in feedback_service.get_bad_cases(limit=limit)]
