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
from typing import Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from api import streaming
from api.deps import (
    describe_scope,
    get_current_user,
    resolve_scope_set,
    scope_key,
)
from api.schemas import ChatIn
from services import (
    feedback_service,
    query_rewrite_service,
    rag_service,
    redis_service,
    retrieval_log_service,
    retrieve_service,
    vector_service,
)
from utils.logger import logger

router = APIRouter(prefix="/chat", tags=["问答"])

#: 置信度阈值（与 app.py:297 一致）
CONFIDENCE_THRESHOLD = 0.5

#: **单条引用**的最低分数：低于它的块不进 "引用来源"，只留在 debug 事件里（排障用）。
#:
#: 为什么必须与 ``CONFIDENCE_THRESHOLD`` 分开定义（哪怕当前同为 0.5）——两者判的不是
#: 同一件事：前者看**整次回答的最高分**（决定"要不要拒答"），后者看**每一块自己的分**
#: （决定"这条证据够不够格摆给用户看"）。用户实测（2026-10-13）：问"张嘉亮是谁？"
#: 返回 5 条引用，其中 4 条是 自动驾驶算法.pdf 和 SAGE AI 问答助手.pdf 的第 18/4/12 页
#: ——它们只是 RRF 捞上来的陪跑（分数接近 0），却因为**当时没有任何引用级过滤**而全部
#: 显示出来。合用一个常量的话，将来想把引用收紧到 0.6，会顺手把拒答线一起抬高。
CITATION_THRESHOLD = 0.5

#: 检索层"没有分数"时的哨兵值，与 ``retrieve_service`` 保持一致：关闭 rerank 时它给每个块写
#: ``rerank_score = 1.0``（语义是"这次不做分数门控"，不是"满分"）。
#: ⚠️ 只在文档块里**真的没有**该字段时兜底；缓存命中路径不许用它——
#: 那正是"缓存 = 1.00"的老 bug（现在从缓存里回放 ``redis_service.CACHE_SCORE_KEY``）。
DEFAULT_RERANK_SCORE = 1.0

#: 拒答关键词（app.py:315-320 原样搬运；改这里必须同步改 app.py，否则两端行为会漂移）
REFUSAL_KEYWORDS = [
    "无法回答", "无法直接回答", "无法根据", "无法确定", "无法提供",
    "无法得出", "无法判断", "无法直接得出",
    "未提供", "未包含", "没有包含", "不包含", "没有涉及", "未涉及",
    "很抱歉", "抱歉", "不知道", "不清楚",
]


#: 引用摘录的字符上限：够看清"命中的是哪一段"，又不至于把整页塞进弹窗 / SSE 帧 / 缓存。
EXCERPT_CHARS = 240


def _excerpt(text: str) -> str:
    """引用摘录（溯源弹窗里显示"这次到底命中了哪段话"）。

    先把空白压成单空格：Docling 出来的 Markdown 常带多余换行与缩进，原样塞进弹窗
    会变成一大片空行，看着像"没摘到内容"。
    """
    cleaned = " ".join((text or "").split())
    return cleaned[:EXCERPT_CHARS] + ("…" if len(cleaned) > EXCERPT_CHARS else "")


def _format_confidence(value: float) -> str:
    """置信度文案（两位小数）：**0.995 以上不许舍入成 1.00**。

    1.00 在本系统里是"这次没做分数门控"的**哨兵值**（关闭精排时检索层就给每个块写 1.0，
    见 ``DEFAULT_RERANK_SCORE``）。真实打分已被 ``utils.rerank.MAX_RERANK_SCORE`` 封顶在
    0.999，但 ``f"{0.999:.2f}"`` 还是 "1.00"——**展示这一步必须再退一格**，
    否则"完美匹配"和"根本没打分"在界面上长得一模一样（用户就是这么被误导的）。
    前端 ``client.ts:formatConfidence`` 是同一条规则的另一份实现。
    """
    shown = f"{float(value):.2f}"
    return "0.99" if shown == "1.00" else shown


def _display_score(payload: ChatIn, max_score: Optional[float]) -> Optional[float]:
    """写进 ``done`` 事件的置信度：**不是真分数就发 None**，绝不发 1.00。

    两类数字都不是"置信度"，一律按"未知"处理（前端不渲染置信度标签）：

    * ``enable_rerank=False`` —— 检索层按设计给每个块写哨兵 1.0（"这次不做分数门控"），
      它既不是满分也不能当置信度用（1.00 会被读成"完美匹配"，这正是用户报的现象）；
    * 值恰好等于哨兵 1.0（老文档缺 ``rerank_score`` 字段 / 命中老缓存里存的 1.0）。

    发 ``None`` 与"命中老缓存但没存分数"走的是**同一条前端路径**（都不渲染），
    比"兜一个 1.0 出来"诚实得多。
    """
    if max_score is None:
        return None
    value = round(float(max_score), 4)
    if not payload.enable_rerank or value >= DEFAULT_RERANK_SCORE:
        return None
    return value


def _build_citations_and_debug(docs, default_scope: Optional[str] = None) -> Tuple[List[Dict], List[Dict]]:
    """引用过滤 + 去重 + 调试块信息（``app.py:327-344`` + 模块 4 的库标注 + 引用噪声过滤）。

    去重键是 ``(展示文件名, 页码, 库)``：同一个父块被多个子块命中时不应重复，
    但**同一个文件同时存在于公共库与私人库时两条都要留**——合并掉就等于把
    "这条证据来自哪个库"的信息丢了，而那正是模块 4 要让用户看到的东西。

    过滤（本次修复）：``rerank_score < CITATION_THRESHOLD`` 的块**不进 citations**。
    为什么必须在**引用层**再过一道（``_sse_stream`` 里已经有一道"整次回答要不要拒答"）：
    拒答看的是最高分，只要有一条像样的证据就会照常回答；而**陪跑的块分数接近 0 却照样
    被列成"引用来源"**，用户看到的就是"这答案引用了自动驾驶算法.pdf"。分数是逐块给的，
    过滤也必须逐块做。

    ``debug_info`` **仍然包含被过滤掉的块**（并带上它们的分数）：排查"为什么这条没进引用"
    时，最先要看的就是"它当时打了多少分"，把证据一起丢掉只会让人重新猜。
    """
    citations: List[Dict] = []
    debug_info: List[Dict] = []
    seen: Dict[tuple, Dict] = {}
    for doc in docs:
        source = doc.metadata.get("source", "未知")
        page = int(doc.metadata.get("page", 0)) + 1
        display_name = source.split("__")[-1] if "__" in source else source
        # 库归属：多库检索时由 retrieve_across_scopes 显式打标；单库检索时上游 metadata
        # 里未必有这个字段（Chroma 侧不保证），用本次唯一的 scope 兜底。
        scope = doc.metadata.get("scope") or default_scope
        score = float(doc.metadata.get("rerank_score", DEFAULT_RERANK_SCORE))
        debug_info.append(
            {
                "length": len(doc.page_content),
                "is_parent": doc.metadata.get("parent_id") is not None,
                "preview": doc.page_content[:150],
                "rerank_score": score,
                "scope": scope,
                "cited": score >= CITATION_THRESHOLD,
            }
        )
        if score < CITATION_THRESHOLD:
            continue
        ref_key = (display_name, page, scope)
        existing = seen.get(ref_key)
        if existing is None:
            entry = {
                "source": display_name,
                "page": page,
                "scope": scope,
                # 分数随引用一起发给前端：前端据此做**防御性**过滤（老后端 / 缓存里的引用
                # 可能没有这个字段，那时前端按"置信度未知"处理，不隐藏也不编数字）。
                "rerank_score": round(score, 4),
                # 溯源弹窗（文件管理 / 下载）要的两样：点开引用后能知道**是哪个文件**
                # （``file_uuid`` → 下载接口只认它）与**命中了哪段原文**（``excerpt``）。
                # 没有它们，弹窗只能重复"文件名 + 第 N 页"，用户还得自己翻到那一页去找。
                "file_uuid": doc.metadata.get("file_uuid"),
                "excerpt": _excerpt(doc.page_content),
            }
            seen[ref_key] = entry
            citations.append(entry)
        elif round(score, 4) > existing["rerank_score"]:
            # 同一个父块被多个子块命中（去重键相同）：留**最高**那次的分数，别让先到者
            # 的偶然低分把整条引用的可信度盖掉；摘录跟着一起换成命中更好那一段，
            # 否则会出现"分数是最好那次、摘录却是最差那次"的自相矛盾。
            existing["rerank_score"] = round(score, 4)
            existing["excerpt"] = _excerpt(doc.page_content)
            existing["file_uuid"] = doc.metadata.get("file_uuid") or existing.get("file_uuid")

    return citations, debug_info


def _done_payload(
    payload: ChatIn,
    scope: str,
    rewritten: str,
    answer: str,
    citations: List[Dict],
    cached: bool,
    max_score: Optional[float],
    refused: bool,
    scopes: Optional[List[str]] = None,
) -> Dict:
    """``done`` 事件的载荷：前端可整段替换（不必依赖逐 token 拼接的结果）。

    ``max_score`` 允许 ``None``：命中**老缓存**（本次改造前写入、没存分数）时，
    宁可让前端不渲染置信度，也不能编一个 1.0 出来。

    ``scope`` 是**库 key**（单库时就是 ``public`` / ``private:<uuid>``，
    多库时是 ``public+private:<uuid>``，与缓存/历史用的是同一个 key）；
    ``scopes``（模块 4）是本次**真正检索过**的库列表——空库已被剔除，
    前端据此渲染「检索范围：公共库 + 我的文档」。

    ⚠️ ``scopes`` 必须放在参数表**最后**并带默认值：本函数早于模块 4 就被
    ``tests/unit/test_semantic_cache.py`` 以位置参数调用（那组测试锁的是分数口径），
    在中间插参数会让它们全部变成 ``TypeError``，看起来像"分数逻辑坏了"。
    """
    return {
        "question": payload.question,
        "rewritten_question": rewritten,
        "answer": answer,
        "citations": citations,
        "scope": scope,
        "scopes": list(scopes or []),
        "cached": cached,
        # 不是真分数（关闭精排的哨兵 1.0 / 文档缺字段时的兜底 1.0）一律发 None：
        # 与"命中老缓存但没存分数"走同一条前端路径（都不渲染置信度），绝不把哨兵
        # 当"满分"展示给用户——详见 ``_display_score``。
        "max_score": _display_score(payload, max_score),
        "refused": refused,
        # 模块 6：👍/👎 的幂等键。由**后端**算，且与 ``POST /feedback`` 用的是同一个
        # ``feedback_service.question_hash``——前端自己算哈希就得在浏览器里再实现一套
        # 归一化，两份实现漂移的表现是"反馈过了按钮还能点，点了又插一行"。
        "q_hash": feedback_service.question_hash(payload.question),
    }


def _retrieved_file_uuids(docs: List) -> List[str]:
    """本次检索命中的文件 uuid（去重、保序）——运营看板算"被检索最多的文档"用。"""
    uuids: List[str] = []
    for doc in docs:
        file_uuid = doc.metadata.get("file_uuid")
        if file_uuid and file_uuid not in uuids:
            uuids.append(file_uuid)
    return uuids


def _log_retrieval(
    payload: ChatIn,
    user: Dict,
    scope_key: str,
    rewritten: str,
    docs: List,
    max_score: float,
    no_result: bool,
) -> None:
    """记一条检索流水（模块 6 的运营看板数据源）。

    三个刻意的取舍：

    * **缓存命中那次不记**：那次压根没有检索（调用点在缓存分支之后），记进去会让
      热门问题的缓存命中把"被检索最多的文档"榜刷成"缓存命中最多的文档"；
    * 这里传的是**原始分**，不是发给前端的那个"哨兵值一律 None"的版本：排障时最想
      知道的就是"当时到底多少分"——0.42 与"没做分数门控"在流水里必须分得开；
    * 写失败不影响问答：``retrieval_log_service.record`` 内部全包（见其 docstring）。
    """
    retrieval_log_service.record(
        user_uuid=user["user_uuid"],
        scope=scope_key,
        question=payload.question,
        rewritten_question=rewritten,
        hit_file_uuids=_retrieved_file_uuids(docs),
        hit_count=len(docs),
        max_score=max_score,
        no_result=no_result,
    )


def _sse_stream(payload: ChatIn, user: Dict, scopes: List[str], scope_key: str) -> Iterator[str]:
    """把一次问答变成 SSE 事件流（同步生成器 → Starlette 会在线程池里迭代，不阻塞事件循环）。

    ``scopes`` 是已校验通过的可信库列表（模块 4：``both`` → ``[public, private:<uuid>]``），
    ``scope_key`` 是它的合并 key，缓存 / 历史 / 日志三处共用同一个 key（见 ``deps.scope_key``）。
    """
    filter_key = payload.file_uuid or "all"
    # 单库时用本次唯一的 scope 给引用兜底打标；多库时一律用块自带的 scope（检索层已显式打标）
    default_scope = scopes[0] if len(scopes) == 1 else None
    try:
        # 空库必须先剔除，不能"反正查不到东西"就不管：
        # Chroma 空集合在部分版本上查询会抛底层错误（hnsw segment reader: Nothing found
        # on disk，见 README 已知限制）；而"私人库还是空的"正是 both 模式最常见的状态。
        vectorstores: Dict[str, object] = {}
        empty_scopes: List[str] = []
        for scope in scopes:
            store = vector_service.get_vectorstore(scope)
            if store.get().get("documents"):
                vectorstores[scope] = store
            else:
                empty_scopes.append(scope)

        if not vectorstores:
            names = "、".join(describe_scope(s) for s in scopes)
            yield streaming.sse_error(f"「{names}」里还没有文档，请先上传文档后再提问")
            return

        searched = list(vectorstores.keys())
        history = redis_service.get_history(user["user_uuid"], scope_key)
        rewritten = query_rewrite_service.rewrite_query(payload.question, history)
        yield streaming.sse_meta(
            # scope = 库 key（前端历史/反馈回传时原样带回，保证同一模式下命中同一份缓存与历史）
            scope=scope_key,
            # scopes = 本次真正检索过的库；skipped_scopes = 因空库被跳过的（排障用旁观字段）
            scopes=searched,
            skipped_scopes=empty_scopes,
            question=payload.question,
            rewritten_question=rewritten,
            rewritten=rewritten != payload.question,
            # 排障/验收用的旁观字段：no_history（一次 LLM 都没调，模块 1）/
            # self_contained（问题本身已完整，本次新增的过度改写防护）。
            # 前端不依赖它，只是让"为什么没改写"可观测，而不是只能靠猜。
            rewrite_skipped=query_rewrite_service.skip_reason(payload.question, history),
        )

        cache = redis_service.get_cached_answer(scope_key, rewritten, filter_key=filter_key)
        if cache:
            answer = cache.get("answer", "")
            citations = cache.get("citations") or []
            debug_info = cache.get("debug_info") or []
            # 回放首次回答的原始置信度（老缓存没存 → None → 前端不显示），绝不写死 1.0
            cached_score = cache.get(redis_service.CACHE_SCORE_KEY)
            yield streaming.sse_meta(cached=True)
            yield streaming.sse_token(answer)
            if citations:
                yield streaming.sse_citations(citations)
            if debug_info:
                yield streaming.sse_debug(debug_info)
            yield streaming.sse_done(
                _done_payload(
                    payload, scope_key, rewritten, answer, citations, True, cached_score,
                    False, scopes=searched,
                )
            )
            redis_service.append_history(user["user_uuid"], scope_key, payload.question, answer)
            logger.info(
                "chat_cache_hit",
                extra={"scope": scope_key, "filter": filter_key, "max_score": cached_score},
            )
            return

        # 单库与多库走**同一个**函数：单库只是"列表里只有一个库"的特例，
        # 这样两条路径的召回/融合/精排逻辑不会各写一份而慢慢漂移。
        docs = retrieve_service.retrieve_across_scopes(
            query=rewritten,
            scopes=searched,
            vectorstores=vectorstores,
            k=10,
            enable_rerank=payload.enable_rerank,
            model_name=payload.rerank_model,
            file_uuid_filter=payload.file_uuid,
        )
        max_score = max(
            (doc.metadata.get("rerank_score", DEFAULT_RERANK_SCORE) for doc in docs),
            default=0.0,
        )

        if not docs or max_score < CONFIDENCE_THRESHOLD:
            # 模块 6：**没结果也要记流水**——"哪些问题检索不到东西"正是运营最缺的信号
            # （它直接告诉你该补什么资料），而那正是改造前随 SSE 连接一起消失的信息。
            _log_retrieval(payload, user, scope_key, rewritten, docs, max_score, True)
            answer = (
                f"⚠️ 未在知识库中找到足够相关的信息（最高置信度 {_format_confidence(max_score)}，"
                f"低于阈值 {CONFIDENCE_THRESHOLD}）。请尝试换个问法，或补充相关资料。"
            )
            yield streaming.sse_token(answer)
            yield streaming.sse_done(
                _done_payload(
                    payload, scope_key, rewritten, answer, [], False, max_score, True,
                    scopes=searched,
                )
            )
            redis_service.append_history(user["user_uuid"], scope_key, payload.question, answer)
            return

        parts: List[str] = []
        for piece in rag_service.generate_answer_stream(rewritten, docs):
            parts.append(piece)
            yield streaming.sse_token(piece)
        answer = "".join(parts)

        # 模块 6：正常路径也记一条流水（no_result=False）。位置在"确实检索并生成"之后：
        # 上面那些提前 return 的分支（空库 / 缓存命中）压根没检索，记了只会污染统计。
        _log_retrieval(payload, user, scope_key, rewritten, docs, max_score, False)

        citations, debug_info = _build_citations_and_debug(docs, default_scope=default_scope)
        refused = any(kw in answer for kw in REFUSAL_KEYWORDS)
        if refused:
            # 拒答：不给引用、也不写缓存（缓存拒答会让一次误判长期生效）
            citations, debug_info = [], []
            yield streaming.sse_meta(refused=True)
        else:
            redis_service.set_cached_answer(
                scope_key, rewritten, answer, citations, debug_info,
                filter_key=filter_key, rerank_score=max_score,
            )

        redis_service.append_history(user["user_uuid"], scope_key, payload.question, answer)
        if citations:
            yield streaming.sse_citations(citations)
        if debug_info:
            yield streaming.sse_debug(debug_info)
        yield streaming.sse_done(
            _done_payload(
                payload, scope_key, rewritten, answer, citations, False, max_score, refused,
                scopes=searched,
            )
        )
        logger.info(
            "chat_completed",
            extra={"scope": scope_key, "docs": len(docs), "max_score": round(max_score, 3)},
        )
    except Exception as e:
        # 流已开始 → 状态码改不了，只能用 error 事件把原因告诉前端
        logger.error("chat_stream_failed", extra={"err": str(e), "scope": scope_key})
        yield streaming.sse_error(f"处理失败：{e}")


@router.post(
    "/stream",
    summary="流式问答（SSE：meta / token / citations / debug / done / error）",
    response_class=StreamingResponse,
)
def chat_stream(payload: ChatIn, user=Depends(get_current_user)) -> StreamingResponse:
    """流式问答。

    权限：普通用户只能问 ``public``、自己私人库，以及 ``both``（= 前两者一起检，
    模块 4）；scope 校验一律由 ``resolve_scope_set`` 兜底，客户端构造任何越界 scope
    都会在进入检索之前拿到 403/400。
    ``file_uuid`` 只是"缩小范围"的过滤条件，即使传别人的 file_uuid，
    scope 过滤仍会让它查不到任何东西，不存在越权读取。
    """
    scopes = resolve_scope_set(payload.scope, user)
    return StreamingResponse(
        _sse_stream(payload, user, scopes, scope_key(scopes)),
        media_type="text/event-stream",
        headers=streaming.SSE_HEADERS,
    )

