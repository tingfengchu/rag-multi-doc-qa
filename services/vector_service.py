import os
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from config import settings

embeddings = OpenAIEmbeddings(
    model="embedding-3",
    api_key=settings.zhipu_api_key,
    base_url=settings.openai_api_base,
)

def get_vectorstore(scope_key: str) -> Chroma:
    """根据 scope 获取或创建对应的 Chroma 向量库"""
    # 🛡️ 关键修复：把 scope 里的冒号替换成下划线，避免 Windows 路径报错
    safe_scope_key = scope_key.replace(":", "_")
    persist_path = os.path.join(settings.base_dir, f"chroma_{safe_scope_key}")
    return Chroma(
        embedding_function=embeddings,
        persist_directory=persist_path,
    )

def add_documents(vectorstore: Chroma, documents):
    vectorstore.add_documents(documents)

def delete_by_source(vectorstore: Chroma, source: str):
    try:
        vectorstore.delete(where={"source": source})
        print(f"  ✓ Chroma 向量已删除: {source}")
    except Exception as e:
        print(f"  ✗ Chroma 删除失败: {e}")