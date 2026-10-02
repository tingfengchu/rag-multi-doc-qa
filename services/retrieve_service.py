import jieba
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever          
from langchain_classic.retrievers import ContextualCompressionRetriever  
from langchain_classic.retrievers.document_compressors import CrossEncoderReranker  
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

def get_hybrid_retriever(vectorstore, splits, k=10):
    """
    构建 BM25 + 向量混合检索器。
    splits 是内存里存的 Document 列表。
    """
    # 1. 向量检索
    vector_retriever = vectorstore.as_retriever(search_kwargs={"k": k})

    # 2. BM25 关键词检索（对中文切词）
    bm25_retriever = BM25Retriever.from_documents(
        splits,
        preprocess_func=lambda text: list(jieba.cut_for_search(text))
    )
    bm25_retriever.k = k

    # 3. 组合检索器（权重各占一半）
    ensemble_retriever = EnsembleRetriever(
        retrievers=[bm25_retriever, vector_retriever],
        weights=[0.5, 0.5]
    )
    return ensemble_retriever

def get_reranked_retriever(vectorstore, splits):
    """
    在混合检索的基础上，加入 Cross-Encoder Rerank。
    """
    ensemble_retriever = get_hybrid_retriever(vectorstore, splits)
    
    # 使用轻量级的 cross-encoder 模型进行重排序
    model = HuggingFaceCrossEncoder(model_name="BAAI/bge-reranker-base")
    compressor = CrossEncoderReranker(model=model, top_n=3)
    
    compression_retriever = ContextualCompressionRetriever(
        base_compressor=compressor,
        base_retriever=ensemble_retriever
    )
    return compression_retriever