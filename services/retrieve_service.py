"""混合检索：ES BM25 + Chroma 向量 ➡️ RRF 融合 ➡️ Cross-Encoder 精排 ➡️ 父块回溯。

说明：Cross-Encoder 模型（约 80MB）只应该加载一次，所以这里用
``utils.caching.resource_cache`` 而不是裸写 ``st.cache_resource``：后者在 Streamlit 之外
（uvicorn 跑 FastAPI / 脚本 / pytest）会退化成普通函数，等于"每个请求都重新加载一遍模型"
——接口能通、日志正常，只是慢到不可用。Streamlit 下行为与改造前完全一致。
"""
import hashlib
from typing import Dict, List, Optional

from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langchain_core.documents import Document

from services import es_service
from utils.caching import resource_cache
from utils.logger import logger
from utils.rerank import (
    DEFAULT_RERANK_MODEL,
    RERANK_MIN_SPREAD,
    is_informative,
    normalize_scores,
    sigmoid,
)

#: 最终交给生成链路的上下文条数（父块数）。
#:
#: 为什么是 5 而不是 3（2026-10-12 实测，RAGAS 前 5 题）：
#:     等权 RRF 有个结构性特点——**只在一个臂命中的文档会被"两个臂都命中"的
#:     中等文档压过**（BM25 第 1 名 = 1/61 ≈ 0.0164 < 1/61+1/62 ≈ 0.0325）。
#:     实测第 4/5 题的正确答案块正是这种情况：
#:         Q5 答案块：BM25 第 1（score 20.72），向量臂 top-10 未命中 → RRF 第 4
#:         Q4 答案块：BM25 第 2（score  5.83），向量臂 top-10 未命中 → RRF 第 5
#:     取 top-3 时这两块都被丢掉，模型只能回答"文档中未提供该信息"，
#:     造成 context_recall=0、answer_relevancy=0。取 top-5 后两者都能进入上下文。
#:
#: 同时它也决定精排区分度的判定窗口：只有"决定输出的这几条"分得开，
#: 精排才值得信任（见 utils/rerank.py 的 window 说明）。
TOP_N_FOR_CONTEXT = 5


@resource_cache
def get_cross_encoder_model(model_name: str):
    # 走 logger 而不是 print：Windows 控制台若是 GBK，print 里的 emoji 会抛
    # UnicodeEncodeError；本函数在"模型首次加载"路径上，异常会把当前请求整个毁掉
    # （SSE 侧表现为 event: error，详见 api/main.py 里的说明）。
    logger.info("cross_encoder_first_load", extra={"model": model_name})
    return HuggingFaceCrossEncoder(model_name=model_name)


def _sigmoid(x):
    """保留的旧函数名（实现已下沉到 ``utils.rerank.sigmoid``，便于离线单测）。"""
    return sigmoid(x)


#: 每个库的候选池大小：BM25 与向量各取 k 条 → RRF 合并后截断到这里 → 交给精排。
#: 与精排一次能吃的候选数一致（见 ``rerank_documents``）。
CANDIDATES_PER_SCOPE = 10


def _bm25_candidates(query: str, scope: str, k: int, file_uuid_filter: Optional[str]) -> List[Document]:
    """ES BM25 关键词召回（多租户隔离靠 ``scope`` 过滤，见 ``es_service.search_bm25``）。"""
    docs: List[Document] = []
    for hit in es_service.search_bm25(query, scope, top_k=k, file_uuid_filter=file_uuid_filter):
        src = hit["_source"]
        meta = src.get("metadata", {}).copy()
        if "parent_id" in src:
            meta["parent_id"] = src["parent_id"]
        docs.append(Document(page_content=src["content"], metadata=meta))
    return docs


def _vector_candidates(query: str, vectorstore, k: int, file_uuid_filter: Optional[str]) -> List[Document]:
    """Chroma 向量召回（``file_uuid`` 过滤在向量库侧做，与 BM25 侧同一条过滤条件）。"""
    if file_uuid_filter:
        return vectorstore.similarity_search(query, k=k, filter={"file_uuid": file_uuid_filter})
    return vectorstore.similarity_search(query, k=k)


def _doc_key(doc: Document) -> str:
    """RRF 去重键：内容哈希（同一个块被两个臂召回时必须合并成一条）。"""
    return hashlib.md5(doc.page_content.encode("utf-8")).hexdigest()


def _rrf_merge(doc_lists: List[List[Document]], rrf_k: int = 60) -> List[Document]:
    """RRF 融合：把多个召回臂的排名压成一个顺序（等权，不需要分数可比）。"""
    rrf_scores, doc_map = {}, {}
    for docs in doc_lists:
        for rank, doc in enumerate(docs, start=1):
            key = _doc_key(doc)
            doc_map[key] = doc
            rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (rrf_k + rank)
    sorted_keys = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    return [doc_map[k] for k in sorted_keys]


def get_hybrid_retriever_with_es(query: str, scope: str, vectorstore,
                                  k: int = 10, enable_rerank: bool = True,
                                  model_name: str = DEFAULT_RERANK_MODEL,
                                  file_uuid_filter: str = None):
    """（单库）ES BM25 + Chroma 向量 ➡️ RRF ➡️ Cross-Encoder 精排 ➡️ 父块回溯

    多库版本见 ``retrieve_across_scopes``（模块 4）：它复用下面同一套阶段函数，
    保证"只检一个库"与"同时检两个库"的打分逻辑完全一致、不会各自漂移。
    """
    candidates = _rrf_merge([
        _bm25_candidates(query, scope, k, file_uuid_filter),
        _vector_candidates(query, vectorstore, k, file_uuid_filter),
    ])[:CANDIDATES_PER_SCOPE]

    scored = rerank_documents(query, candidates, enable_rerank=enable_rerank, model_name=model_name)
    final_docs = scored[:TOP_N_FOR_CONTEXT]
    return _expand_parents(final_docs) or final_docs

def rerank_documents(query: str, docs: List[Document], enable_rerank: bool = True,
                     model_name: str = DEFAULT_RERANK_MODEL,
                     max_candidates: int = CANDIDATES_PER_SCOPE) -> List[Document]:
    """Cross-Encoder 精排 + **按模型族归一化**的分数，返回按分数排序的候选（分数写回 ``rerank_score``）。

    抽成独立函数是为了多库检索（模块 4）：合并后的候选**必须一次性全局精排**，
    否则每个库各自归一化（min-max）后，"A 库第 3 名"与"B 库第 1 名"的分数不可比，
    合并排序会变成按库轮流取第一条，跨库的强证据反而被压下去。
    """
    if not docs:
        return []

    if not enable_rerank:
        # 关闭精排：每个块写 1.0，语义是"这次不做分数门控"（不是满分），调用方按 RRF 顺序取
        for doc in docs:
            doc.metadata["rerank_score"] = 1.0
        return docs

    model = get_cross_encoder_model(model_name)
    top_docs = docs[:max_candidates]
    pairs = [[query, doc.page_content] for doc in top_docs]
    raw_scores = [float(score) for score in model.score(pairs)]

    # 🚨 区分度闸门：精排只有在"确实能区分"时才允许覆盖 RRF 顺序。
    # 实测反例（2026-10 审计）：英文模型 cross-encoder/ms-marco-MiniLM-L-6-v2
    # 给中文问句打分时，前 3 名的 sigmoid 分数是 0.99982 / 0.99918 / 0.99915
    # （极差 0.0007）——"哪 3 条被选中"完全等价于随机抽签，
    # BM25 排第 1、RRF 也是第 1 的简历联系方式块就这样被挤出 top-3，
    # 直接导致 context_recall=0、模型只能答"文档中未提供该信息"。详见 utils/rerank.py。
    #
    # 注意窗口：判据必须落在"决定输出的前 TOP_N 条"上。只算全局极差会漏判——
    # top-10 里混着明显无关的低分文档，整体极差很大，但前 3 名之间其实分不开。
    # model_name 必须一路传下去：门控与展示用的是**同一套模型族映射**，但温度不同
    # （门控 T=1 最灵敏、展示 T=5 最可读），否则两处口径漂移就再也解释不清分数。
    trustworthy, spread = is_informative(
        raw_scores, window=TOP_N_FOR_CONTEXT, model_name=model_name
    )
    normalized = normalize_scores(raw_scores, model_name=model_name)

    if trustworthy:
        scored = sorted(zip(top_docs, normalized), key=lambda item: item[1], reverse=True)
    else:
        # 这里以前还有一段 print（带 ⚠️ emoji，内容是下面这条日志 + 换模型建议）。
        # print 的 emoji 在 GBK 控制台下会抛 UnicodeEncodeError，而本函数是在
        # "一次问答的检索路径"上，异常会直接毁掉这次回答（SSE 侧只能发 event: error）；
        # 所以统一走 logger，把建议放进 hint 字段。
        logger.warning(
            "rerank_no_discrimination",
            extra={
                "model": model_name,
                "spread": spread,
                "min_spread": RERANK_MIN_SPREAD,
                "candidates": len(top_docs),
                "hint": "已保留 RRF 顺序；中文语料建议改用 BAAI/bge-reranker-base",
            },
        )
        scored = list(zip(top_docs, normalized))

    for doc, score in scored:
        doc.metadata["rerank_score"] = score
    return [doc for doc, _ in scored]


def _expand_parents(docs: List[Document]) -> List[Document]:
    """父块回溯：把命中的子块换成完整父块（同一个父块只保留一次）。"""
    parent_docs, seen = [], set()
    for doc in docs:
        pid = doc.metadata.get("parent_id")
        if pid and pid not in seen:
            seen.add(pid)
            data = es_service.get_parent_chunk(pid)
            if data:
                pmeta = data.get("metadata", {}).copy()
                pmeta["parent_id"] = pid
                pmeta["rerank_score"] = doc.metadata.get("rerank_score", 1.0)
                # 子块上显式打的库标记优先：它是"这条引用属于哪个库"的唯一依据
                # （ES 里父块的 scope 通常也在，但多库检索时以子块的为准，避免串库）。
                if doc.metadata.get("scope"):
                    pmeta["scope"] = doc.metadata["scope"]
                parent_docs.append(Document(page_content=data["content"], metadata=pmeta))
    return parent_docs


def retrieve_across_scopes(query: str, scopes: List[str], vectorstores: Dict[str, object],
                           k: int = 10, enable_rerank: bool = True,
                           model_name: str = DEFAULT_RERANK_MODEL,
                           file_uuid_filter: str = None) -> List[Document]:
    """模块 4：多库检索（``public`` + 自己的私人库）。

    流程：每个库各自跑 BM25 + 向量 + RRF 得到候选池 ➡️ 合并去重 ➡️ **全局精排一次**
    ➡️ 取 top-``TOP_N_FOR_CONTEXT`` ➡️ 父块回溯。每个返回块的 ``metadata["scope"]``
    就是它来自哪个库，前端据此在引用上标注「公共库 / 我的文档」。

    为什么"每库各召回 + 全局精排"：
    * 先按库召回，是因为 BM25 与向量检索都必须带 ``scope`` 过滤（多租户隔离），
      不能先把两个库的结果混成一个池子再过滤——那等于把隔离交给了后置步骤；
    * 精排放在合并之后，是因为它会做*归一化*，分库精排会让跨库分数不可比（见 ``rerank_documents``）。

    ``vectorstores`` 只包含**确实有文档**的库（空库会被调用方跳过：Chroma 空集合
    在某些版本上查询会抛 ``hnsw segment reader`` 之类的底层错误，见 README 已知限制）。
    """
    candidates: List[Document] = []
    per_scope_counts: Dict[str, int] = {}

    for scope in scopes:
        vectorstore = vectorstores.get(scope)
        if vectorstore is None:
            continue
        docs = _rrf_merge([
            _bm25_candidates(query, scope, k, file_uuid_filter),
            _vector_candidates(query, vectorstore, k, file_uuid_filter),
        ])[:CANDIDATES_PER_SCOPE]
        for doc in docs:
            # 显式打标，而不是指望上游 metadata 里一定有 scope：
            # "这条引用来自哪个库"是给用户看的标注，不能依赖某个库的写入实现是否完整。
            doc.metadata["scope"] = scope
        candidates.extend(docs)

    # 跨库去重：同一个块可能同时存在于两个库（用户把公共文档又传了一份到私人库），
    # 去重键必须带 scope——否则私人库那份会被"顺手删掉"，引用标注也就丢了来源。
    unique: List[Document] = []
    seen = set()
    for doc in candidates:
        scope = doc.metadata.get("scope", "")
        key = (scope, _doc_key(doc))
        if key in seen:
            continue
        seen.add(key)
        unique.append(doc)

    scored = rerank_documents(
        query, unique, enable_rerank=enable_rerank, model_name=model_name,
        # 候选数按库数放大：否则后一个库的候选永远进不了精排窗口（等于没检它）
        max_candidates=CANDIDATES_PER_SCOPE * max(1, len(scopes)),
    )
    final_docs = scored[:TOP_N_FOR_CONTEXT]
    docs = _expand_parents(final_docs) or final_docs

    for doc in docs:
        scope = doc.metadata.get("scope", "")
        per_scope_counts[scope] = per_scope_counts.get(scope, 0) + 1
    logger.info(
        "retrieve_multi_scope",
        extra={"scopes": list(scopes), "candidates": len(candidates), "kept": per_scope_counts},
    )
    return docs