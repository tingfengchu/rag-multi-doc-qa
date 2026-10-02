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
    persist_path = os.path.join(settings.base_dir, f"chroma_{scope_key}")
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