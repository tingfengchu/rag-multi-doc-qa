import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"  # 强制走国内镜像

from services import vector_service, retrieve_service, rag_service
from langchain_core.documents import Document

# 1. 连接公共库
vs = vector_service.get_vectorstore("public")
all_docs = vs.get()
splits = [
    Document(page_content=text, metadata=meta)
    for text, meta in zip(all_docs['documents'], all_docs['metadatas'])
]

print(f"📚 当前向量库文档总数：{len(splits)}")

# 2. 构建带 Rerank 的检索器
retriever = retrieve_service.get_reranked_retriever(vs, splits)

# 3. 提问并检索
query = "张嘉亮的专业技能里，关于RAG都有什么描述？"
results = retriever.invoke(query)

print(f"\n🔍 检索问题：{query}\n")
for i, doc in enumerate(results, 1):
    print(f"--- 重排后结果 {i} ---")
    print(f"来源: {doc.metadata.get('source')}")
    print(f"内容片段: {doc.page_content[:100]}...\n")

# 4. 将检索结果交给大模型生成最终答案
print("🤖 正在请求智谱大模型生成答案...\n")
answer = rag_service.generate_answer(query, results)
print("=" * 40)
print("最终回答：")
print(answer)
print("=" * 40)