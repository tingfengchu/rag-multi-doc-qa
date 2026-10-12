import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import pytest
from langchain_core.documents import Document
from services import vector_service, retrieve_service, rag_service


@pytest.mark.integration
def test_public_rag_end_to_end():
    """公共库端到端集成测试：检索 + 生成"""
    vs = vector_service.get_vectorstore("public")
    all_docs = vs.get()

    documents = all_docs.get("documents") or []
    metadatas = all_docs.get("metadatas") or []

    if not documents:
        pytest.skip("公共向量库为空，跳过集成测试")

    splits = [
        Document(page_content=text, metadata=meta or {})
        for text, meta in zip(documents, metadatas)
    ]

    query = "张嘉亮的专业技能里，关于RAG都有什么描述？"
    retriever = retrieve_service.get_hybrid_retriever_with_es(
        query=query, scope="public", vectorstore=vs, k=10,
        enable_rerank=True,
        model_name="cross-encoder/ms-marco-MiniLM-L-6-v2",
    )

    assert retriever is not None

    answer = rag_service.generate_answer(query, retriever)
    assert answer
    assert len(answer) > 0