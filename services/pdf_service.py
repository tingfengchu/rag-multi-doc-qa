import os
import tempfile
import pymupdf4llm
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
PAGE_OFFSET = 1

def process_uploaded_pdf(uploaded_file, owner_uuid: str, file_uuid: str, source_name: str):
    """
    解析上传的 PDF，返回切分好的 Document 列表。
    source_name 由外部生成并传入，不再由本函数拼接。
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    try:
        try:
            page_dicts = pymupdf4llm.to_markdown(tmp_path, page_chunks=True)
        except Exception as e:
            print(f"❌ PDF 解析失败：{e}")
            return []

        docs = []
        for p in page_dicts:
            meta = p.get("metadata", {})
            raw_page = meta.get("page", meta.get("page_number", meta.get("page_num", 0)))
            page_num = max(int(raw_page) - PAGE_OFFSET, 0)
            
            docs.append(Document(
                page_content=p["text"],
                metadata={
                    "source": source_name, 
                    "page": page_num,
                    "owner_uuid": owner_uuid,
                    "file_uuid": file_uuid,
                }
            ))

        headers_to_split_on = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
        md_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on, strip_headers=False)
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)

        new_splits = []
        for doc in docs:
            md_chunks = md_splitter.split_text(doc.page_content)
            for chunk in md_chunks:
                chunk.metadata.update(doc.metadata)
                if len(chunk.page_content) > 1500:
                    sub_chunks = text_splitter.split_documents([chunk])
                    for sub in sub_chunks:
                        sub.metadata.update(chunk.metadata)
                        new_splits.append(sub)
                else:
                    new_splits.append(chunk)
                    
        return new_splits

    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError as e:
            print(f"临时文件清理失败（忽略）：{e}")