"""混合检索：ES BM25 + Chroma 向量 ➡️ RRF 融合 ➡️ Cross-Encoder 精排 ➡️ 父块回溯。

说明：Cross-Encoder 模型（约 80MB）只应该加载一次，所以这里用
``utils.caching.resource_cache`` 而不是裸写 ``st.cache_resource``：后者在 Streamlit 之外
（uvicorn 跑 FastAPI / 脚本 / pytest）会退化成普通函数，等于"每个请求都重新加载一遍模型"
——接口能通、日志正常，只是慢到不可用。Streamlit 下行为与改造前完全一致。
"""
import hashlib

from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langchain_core.documents import Document

from services import es_service
from utils.caching import resource_cache
from utils.logger import logger
from utils.rerank import RERANK_MIN_SPREAD, is_informative, normalize_scores, sigmoid

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
    print(f"⏳ 首次加载 Cross-Encoder 模型: {model_name}")
    return HuggingFaceCrossEncoder(model_name=model_name)


def _sigmoid(x):
    """保留的旧函数名（实现已下沉到 ``utils.rerank.sigmoid``，便于离线单测）。"""
    return sigmoid(x)


def get_hybrid_retriever_with_es(query: str, scope: str, vectorstore,
                                  k: int = 10, enable_rerank: bool = True,
                                  model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                                  file_uuid_filter: str = None):
    """ES BM25 + Chroma 向量 ➡️ RRF ➡️ Cross-Encoder 精排 ➡️ 父块回溯"""

    # 1. ES BM25（带 file_uuid 过滤）
    bm25_results = es_service.search_bm25(query, scope, top_k=k, file_uuid_filter=file_uuid_filter)
    bm25_docs = []
    for hit in bm25_results:
        src = hit["_source"]
        meta = src.get("metadata", {}).copy()
        if "parent_id" in src:
            meta["parent_id"] = src["parent_id"]
        bm25_docs.append(Document(page_content=src["content"], metadata=meta))

    # 2. Chroma 向量（带 file_uuid 过滤）
    if file_uuid_filter:
        vector_docs = vectorstore.similarity_search(query, k=k, filter={"file_uuid": file_uuid_filter})
    else:
        vector_docs = vectorstore.similarity_search(query, k=k)

    def _doc_key(doc):
        return hashlib.md5(doc.page_content.encode("utf-8")).hexdigest()
    # 3. RRF 融合
    rrf_k = 60
    rrf_scores, doc_map = {}, {}
    for rank, doc in enumerate(bm25_docs, start=1):
        key = _doc_key(doc)
        doc_map[key] = doc
        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (rrf_k + rank)
    for rank, doc in enumerate(vector_docs, start=1):
        key = _doc_key(doc)
        doc_map[key] = doc
        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (rrf_k + rank)
    sorted_keys = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    merged_docs = [doc_map[k] for k in sorted_keys]

    # 4. Cross-Encoder 重排 + sigmoid 归一化分数
    if enable_rerank and merged_docs:
        model = get_cross_encoder_model(model_name)
        top_docs = merged_docs[:10]
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
        trustworthy, spread = is_informative(raw_scores, window=TOP_N_FOR_CONTEXT)
        normalized = normalize_scores(raw_scores)

        if trustworthy:
            scored = sorted(zip(top_docs, normalized), key=lambda item: item[1], reverse=True)
        else:
            logger.warning(
                "rerank_no_discrimination",
                extra={
                    "model": model_name,
                    "spread": spread,
                    "min_spread": RERANK_MIN_SPREAD,
                    "candidates": len(top_docs),
                },
            )
            print(
                f"⚠️ 精排模型 {model_name} 对本次查询没有区分度"
                f"（归一化分数极差 {spread:.5f} < {RERANK_MIN_SPREAD}），已保留 RRF 顺序。"
                "中文语料建议改用 BAAI/bge-reranker-base。"
            )
            scored = list(zip(top_docs, normalized))

        for doc, score in scored:
            doc.metadata["rerank_score"] = score
        final_docs = [doc for doc, _ in scored[:TOP_N_FOR_CONTEXT]]
    else:
        for doc in merged_docs[:TOP_N_FOR_CONTEXT]:
            doc.metadata["rerank_score"] = 1.0
        final_docs = merged_docs[:TOP_N_FOR_CONTEXT]

    # 5. 父块回溯
    parent_docs, seen = [], set()
    for doc in final_docs:
        pid = doc.metadata.get("parent_id")
        if pid and pid not in seen:
            seen.add(pid)
            data = es_service.get_parent_chunk(pid)
            if data:
                pmeta = data.get("metadata", {}).copy()
                pmeta["parent_id"] = pid
                pmeta["rerank_score"] = doc.metadata.get("rerank_score", 1.0)
                parent_docs.append(Document(page_content=data["content"], metadata=pmeta))

    return parent_docs if parent_docs else final_docs