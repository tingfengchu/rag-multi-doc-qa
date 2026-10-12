"""问答路由：SSE 流式问答（``app.py:252-379`` 的 1:1 迁移）。

为什么是 POST + fetch 而不是 GET + EventSource：浏览器原生 ``EventSource`` 只支持 GET、
也不能自定义请求头；问答需要 POST body（问题 / scope / 文件过滤），所以前端用
``fetch`` + ``ReadableStream`` 解析 SSE。服务端两种方式完全一样（``text/event-stream``），
curl 也能直接调，便于排查。

⚠️ 流一旦开始就无法再改 HTTP 状态码，所以**所有**异常都必须在生成器内部捕获并转成
``event: error``，否则前端只会看到"连接莫名断开"，连原因都拿不到。

缓存与拒答策略（与 Streamlit 版逐条对齐）：
* 命中语义缓存 → 跳过检索与 LLM，直接吐答案；
* 检索最高分 < 阈值 → 固定文案"未找到足够相关信息"；
* 模型输出命中拒答关键词 → 视为拒答，**不给引用、也不写缓存**——
  把拒答内容缓存住会让同样的问题长期拿到"无法回答"，等于把一次误判永久化。
"""
from typing import Dict, Iterator, List, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from api import streaming
from api.deps import get_current_user, resolve_scope
from api.schemas import ChatIn
from services import (
    query_rewrite_service,
    rag_service,
    redis_service,
    retrieve_service,
    vector_service,
)
from utils.logger import logger

router = APIRouter(prefix="/chat", tags=["问答"])

#: 置信度阈值（与 app.py:297 一致）
CONFIDENCE_THRESHOLD = 0.5

#: 拒答关键词（app.py:315-320 原样搬运；改这里必须同步改 app.py，否则两端行为会漂移）
REFUSAL_KEYWORDS = [
    "无法回答", "无法直接回答", "无法根据", "无法确定", "无法提供",
    "无法得出", "无法判断", "无法直接得出",
    "未提供", "未包含", "没有包含", "不包含", "没有涉及", "未涉及",
    "很抱歉", "抱歉", "不知道", "不清楚",
]


def _build_citations_and_debug(docs) -> Tuple[List[Dict], List[Dict]]:
    """引用去重 + 调试块信息（``app.py:327-344``）。

    去重键是 ``(展示文件名, 页码)``：同一个父块被多个子块命中时，
    前端引用列表里不应出现重复条目。
    """
    citations: List[Dict] = []
    debug_info: List[Dict] = []
    seen = set()
    for doc in docs:
        source = doc.metadata.get("source", "未知")
        page = int(doc.metadata.get("page", 0)) + 1
        display_name = source.split("__")[-1] if "__" in source else source
        ref_key = (display_name, page)
        if ref_key not in seen:
            seen.add(ref_key)
            citations.append({"source": display_name, "page": page})
        debug_info.append(
            {
                "length": len(doc.page_content),
                "is_parent": doc.metadata.get("parent_id") is not None,
                "preview": doc.page_content[:150],
                "rerank_score": doc.metadata.get("rerank_score", 1.0),
            }
        )
    return citations, debug_info


def _done_payload(
    payload: ChatIn,
    scope: str,
    rewritten: str,
    answer: str,
    citations: List[Dict],
    cached: bool,
    max_score: float,
    refused: bool,
) -> Dict:
    """``done`` 事件的载荷：前端可整段替换（不必依赖逐 token 拼接的结果）。"""
    return {
        "question": payload.question,
        "rewritten_question": rewritten,
        "answer": answer,
        "citations": citations,
        "scope": scope,
        "cached": cached,
        "max_score": round(float(max_score), 4),
        "refused": refused,
    }


def _sse_stream(payload: ChatIn, user: Dict, scope: str) -> Iterator[str]:
    """把一次问答变成 SSE 事件流（同步生成器 → Starlette 会在线程池里迭代，不阻塞事件循环）。"""
    filter_key = payload.file_uuid or "all"
    try:
        vectorstore = vector_service.get_vectorstore(scope)
        if not vectorstore.get().get("documents"):
            yield streaming.sse_error(f"库 {scope} 还没有文档，请先上传文档")
            return

        history = redis_service.get_history(user["user_uuid"], scope)
        rewritten = query_rewrite_service.rewrite_query(payload.question, history)
        yield streaming.sse_meta(
            scope=scope,
            question=payload.question,
            rewritten_question=rewritten,
            rewritten=rewritten != payload.question,
        )

        cache = redis_service.get_cached_answer(scope, rewritten, filter_key=filter_key)
        if cache:
            answer = cache.get("answer", "")
            citations = cache.get("citations") or []
            debug_info = cache.get("debug_info") or []
            yield streaming.sse_meta(cached=True)
            yield streaming.sse_token(answer)
            if citations:
                yield streaming.sse_citations(citations)
            if debug_info:
                yield streaming.sse_debug(debug_info)
            yield streaming.sse_done(
                _done_payload(
                    payload, scope, rewritten, answer, citations, True, 1.0, False
                )
            )
            redis_service.append_history(user["user_uuid"], scope, payload.question, answer)
            logger.info("chat_cache_hit", extra={"scope": scope, "filter": filter_key})
            return

        docs = retrieve_service.get_hybrid_retriever_with_es(
            query=rewritten,
            scope=scope,
            vectorstore=vectorstore,
            k=10,
            enable_rerank=payload.enable_rerank,
            model_name=payload.rerank_model,
            file_uuid_filter=payload.file_uuid,
        )
        max_score = max(
            (doc.metadata.get("rerank_score", 1.0) for doc in docs), default=0.0
        )

        if not docs or max_score < CONFIDENCE_THRESHOLD:
            answer = (
                f"⚠️ 未在知识库中找到足够相关的信息（最高置信度 {max_score:.2f}，"
                f"低于阈值 {CONFIDENCE_THRESHOLD}）。请尝试换个问法，或补充相关资料。"
            )
            yield streaming.sse_token(answer)
            yield streaming.sse_done(
                _done_payload(payload, scope, rewritten, answer, [], False, max_score, True)
            )
            redis_service.append_history(user["user_uuid"], scope, payload.question, answer)
            return

        parts: List[str] = []
        for piece in rag_service.generate_answer_stream(rewritten, docs):
            parts.append(piece)
            yield streaming.sse_token(piece)
        answer = "".join(parts)

        citations, debug_info = _build_citations_and_debug(docs)
        refused = any(kw in answer for kw in REFUSAL_KEYWORDS)
        if refused:
            # 拒答：不给引用、也不写缓存（缓存拒答会让一次误判长期生效）
            citations, debug_info = [], []
            yield streaming.sse_meta(refused=True)
        else:
            redis_service.set_cached_answer(
                scope, rewritten, answer, citations, debug_info, filter_key=filter_key
            )

        redis_service.append_history(user["user_uuid"], scope, payload.question, answer)
        if citations:
            yield streaming.sse_citations(citations)
        if debug_info:
            yield streaming.sse_debug(debug_info)
        yield streaming.sse_done(
            _done_payload(
                payload, scope, rewritten, answer, citations, False, max_score, refused
            )
        )
        logger.info(
            "chat_completed",
            extra={"scope": scope, "docs": len(docs), "max_score": round(max_score, 3)},
        )
    except Exception as e:
        # 流已开始 → 状态码改不了，只能用 error 事件把原因告诉前端
        logger.error("chat_stream_failed", extra={"err": str(e), "scope": scope})
        yield streaming.sse_error(f"处理失败：{e}")


@router.post(
    "/stream",
    summary="流式问答（SSE：meta / token / citations / debug / done / error）",
    response_class=StreamingResponse,
)
def chat_stream(payload: ChatIn, user=Depends(get_current_user)) -> StreamingResponse:
    """流式问答。

    权限：普通用户只能问 ``public`` 与自己私人库（``resolve_scope`` 兜底）。
    ``file_uuid`` 只是"缩小范围"的过滤条件，即使传别人的 file_uuid，
    scope 过滤仍会让它查不到任何东西，不存在越权读取。
    """
    scope = resolve_scope(payload.scope, user)
    return StreamingResponse(
        _sse_stream(payload, user, scope),
        media_type="text/event-stream",
        headers=streaming.SSE_HEADERS,
    )

