"""混合检索：ES BM25 + Chroma 向量 ➡️ RRF 融合 ➡️ Cross-Encoder 精排 ➡️ 父块回溯。

说明：这里保留 ``st.cache_resource``，因为 Cross-Encoder 模型（约 80MB）
只应该加载一次。在非 Streamlit 环境（脚本 / pytest）里 ``st.cache_resource``
会退化成普通函数并打印一条 warning，功能不受影响。
"""
import hashlib
import math

import streamlit as st
from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langchain_core.documents import Document

from services import es_service


@st.cache_resource
def get_cross_encoder_model(model_name: str):
    print(f"⏳ 首次加载 Cross-Encoder 模型: {model_name}")
    return HuggingFaceCrossEncoder(model_name=model_name)


def _sigmoid(x):
    try:
        return 1 / (1 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


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
        raw_scores = model.score(pairs)
        scored = sorted(zip(top_docs, raw_scores), key=lambda x: x[1], reverse=True)
        for doc, raw in scored:
            doc.metadata["rerank_score"] = _sigmoid(float(raw))
        final_docs = [doc for doc, _ in scored[:3]]
    else:
        for doc in merged_docs[:3]:
            doc.metadata["rerank_score"] = 1.0
        final_docs = merged_docs[:3]

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