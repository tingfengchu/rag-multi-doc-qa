"""反馈路由：提交评价（👍/👎+原因）与 bad case 查询（管理员）。

对应 ``app.py:382-424`` 的反馈飞轮；``bad-cases`` 是 API 新增的能力
（Streamlit 版没有这个入口，但反馈表一直在写数据，一直没人看）。
"""
from typing import List

from fastapi import APIRouter, Depends, Query

from api import errors
from api.deps import get_current_user, require_admin, resolve_scope
from api.schemas import BadCaseOut, FeedbackIn, MessageOut
from services import feedback_service
from utils.logger import logger

router = APIRouter(prefix="/feedback", tags=["反馈"])


@router.post("", response_model=MessageOut, summary="提交一条问答反馈")
def submit_feedback(
    payload: FeedbackIn, user=Depends(get_current_user)
) -> MessageOut:
    # 不信任 body 里的 scope：越权构造 scope 只会污染别人的统计口径
    resolved = resolve_scope(payload.scope, user)

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

    logger.info("feedback_api_ok", extra={"rating": payload.rating, "scope": resolved})
    return MessageOut(message="感谢您的反馈，我们会持续优化！")


@router.get("/bad-cases", response_model=List[BadCaseOut], summary="最近的差评样本（管理员）")
def list_bad_cases(
    limit: int = Query(50, ge=1, le=500),
    user=Depends(require_admin),
) -> List[BadCaseOut]:
    """按时间倒序返回 👎 的样本，供运营定位"答得不好"的问题。"""
    return [BadCaseOut(**row) for row in feedback_service.get_bad_cases(limit=limit)]
