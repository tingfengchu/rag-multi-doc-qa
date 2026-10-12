import logging
import os

from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from config import settings

logger = logging.getLogger("rag.vector")

embeddings = OpenAIEmbeddings(
    model="embedding-3",
    api_key=settings.zhipu_api_key,
    base_url=settings.openai_api_base,
)


def get_vectorstore(scope_key: str) -> Chroma:
    """根据 scope 获取或创建对应的 Chroma 向量库。

    所有 scope 的子库都放在 settings.chroma_root 下，
    这样 Docker 只要挂载一个目录就能保住全部向量数据。
    """
    safe_scope_key = scope_key.replace(":", "_")
    root = settings.chroma_root
    os.makedirs(root, exist_ok=True)
    persist_path = os.path.join(root, f"chroma_{safe_scope_key}")
    return Chroma(
        embedding_function=embeddings,
        persist_directory=persist_path,
    )


def add_documents(vectorstore: Chroma, documents):
    valid_docs = [doc for doc in documents if doc.page_content and doc.page_content.strip()]

    if not valid_docs:
        raise ValueError("❌ 解析或切分后未产生任何有效文本块，请检查文件是否为空白或纯图片。")

    batch_size = 10
    success_count = 0
    fail_count = 0

    for i in range(0, len(valid_docs), batch_size):
        batch = valid_docs[i:i + batch_size]
        try:
            vectorstore.add_documents(batch)
            success_count += len(batch)
            print(f"✅ 成功入库第 {i // batch_size + 1} 批，包含 {len(batch)} 个块")
        except Exception as e:
            print(f"❌ 第 {i // batch_size + 1} 批入库失败，尝试逐个排查: {e}")
            for single_doc in batch:
                try:
                    vectorstore.add_documents([single_doc])
                    success_count += 1
                except Exception as single_e:
                    fail_count += 1
                    print(f"  跳过非法块（内容: {single_doc.page_content[:50]}...）: {single_e}")

    print(f"📊 入库完成：成功 {success_count} 块，失败 {fail_count} 块")
    if success_count == 0:
        raise ValueError("❌ 所有文档块均入库失败，请检查数据或向量库连接。")


def delete_by_source(vectorstore: Chroma, source: str) -> int:
    """删除某个 source 下的所有向量，返回删除条数。

    langchain_chroma 没有"按 metadata 删除"的公开 API，所以先用公开的
    ``get(where=...)`` 拿到 id 列表、再用公开的 ``delete(ids=...)`` 删除，
    避免直接依赖底层 ``_collection`` 在版本升级后静默失效。

    失败时**抛异常**而不是只打印：上传失败回滚、删除文件时，
    如果这里静默失败就会留下永远清不掉的孤儿向量。
    """
    try:
        data = vectorstore.get(where={"source": source})
        ids = data.get("ids") or []
        if not ids:
            logger.info("chroma_delete_no_match", extra={"source": source})
            return 0
        vectorstore.delete(ids=ids)
        logger.info("chroma_deleted", extra={"source": source, "count": len(ids)})
        print(f"  ✓ Chroma 向量已删除: {source}（{len(ids)} 个块）")
        return len(ids)
    except Exception as e:
        logger.error("chroma_delete_failed", extra={"source": source, "err": str(e)})
        raise


def delete_all(vectorstore: Chroma) -> int:
    """清空一个 scope 下的所有向量，返回删除条数（用于重置知识库）。"""
    data = vectorstore.get()
    ids = data.get("ids") or []
    if ids:
        vectorstore.delete(ids=ids)
    logger.info("chroma_deleted_all", extra={"count": len(ids)})
    return len(ids)


def count(vectorstore: Chroma) -> int:
    """当前向量库有多少个块（调试 / 评测用）。"""
    try:
        return len(vectorstore.get().get("ids") or [])
    except Exception as e:
        logger.warning("chroma_count_failed", extra={"err": str(e)})
        return 0