import os
import tempfile
import uuid
import pandas as pd
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from docling.document_converter import DocumentConverter

from utils.caching import resource_cache
from utils.logger import logger

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

# Docling 转换器（含版面/表格模型）构建一次就很贵，必须进程内复用。
# 用 resource_cache 而不是 st.cache_resource：FastAPI(uvicorn) 下后者会退化成普通函数，
# 等于每个上传请求都重建一次转换器。
@resource_cache
def get_docling_converter():
    # 与 retrieve_service 同理：print 里的 emoji 在 GBK 控制台会抛
    # UnicodeEncodeError，而这里是"首次上传"路径，异常会毁掉整个上传请求。
    logger.info("docling_converter_first_load")
    return DocumentConverter()


def _process_excel(uploaded_file, owner_uuid, file_uuid, source_name):
    """🚨 Excel 专用：按行剥离，每行作为一个独立的自然语言子块"""
    file_ext = os.path.splitext(uploaded_file.name)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    try:
        try:
            excel_file = pd.ExcelFile(tmp_path)
        except ImportError as e:
            # .xls(旧格式) 需要 xlrd，.xlsx 需要 openpyxl。
            # 这两个包以前是 docling 的间接依赖，没写进 requirements.txt，
            # 所以在干净环境里上传 Excel 会直接抛一个很难懂的 ImportError。
            raise ValueError(
                f"解析 {file_ext} 缺少依赖（.xls 需要 xlrd，.xlsx 需要 openpyxl），"
                f"请先执行：pip install xlrd openpyxl。原始错误：{e}"
            ) from e
        new_splits = []

        for sheet_name in excel_file.sheet_names:
            df = pd.read_excel(tmp_path, sheet_name=sheet_name, header=0)
            df = df.fillna("")
            headers = [str(c).strip() for c in df.columns]

            # 1. 表头作为一个独立块（用于回答"有哪些字段"这类问题）
            header_text = f"【{sheet_name} 表头字段】\n" + "、".join([h for h in headers if h])
            header_parent_id = str(uuid.uuid4())
            header_doc = Document(
                page_content=header_text,
                metadata={"source": source_name, "page": 0, "owner_uuid": owner_uuid,
                          "file_uuid": file_uuid, "parent_id": header_parent_id,
                          "sheet_name": sheet_name, "row_index": 0}
            )
            new_splits.append({"parent": header_doc, "child": header_doc})

            # 2. 每一行数据作为一个独立的块
            for idx, row in df.iterrows():
                parts = []
                for col, val in zip(headers, row.values):
                    val_str = str(val).strip()
                    if val_str and val_str.lower() != "nan":
                        parts.append(f"{col}：{val_str}")
                if not parts:
                    continue
                row_text = f"【{sheet_name} 第{idx + 2}行】\n" + "；".join(parts)
                parent_id = str(uuid.uuid4())
                row_doc = Document(
                    page_content=row_text,
                    metadata={"source": source_name, "page": 0, "owner_uuid": owner_uuid,
                              "file_uuid": file_uuid, "parent_id": parent_id,
                              "sheet_name": sheet_name, "row_index": int(idx) + 2}
                )
                new_splits.append({"parent": row_doc, "child": row_doc})
        return new_splits
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def _iter_page_markdown(result):
    """按页产出 ``(page_no, markdown)``。

    Docling 的 ``export_to_markdown(page_no=n)`` 可以只导出指定页，这样切出来
    的块就带着**真实页码**。以前是整篇导出、metadata["page"] 固定写 0，
    导致前端引用永远显示"第 1 页"（实测线上 170 个块全是 page=0）。

    任何一步失败都回退为"整篇导出 + 页码 1"，保证不阻塞入库。
    """
    document = getattr(result, "document", None)
    if document is None:
        return

    page_ids = []
    try:
        page_ids = sorted(int(p) for p in (document.pages or {}).keys())
    except Exception:
        page_ids = []

    if page_ids:
        try:
            for page_no in page_ids:
                yield page_no, document.export_to_markdown(page_no=page_no)
            return
        except Exception as e:
            print(f"⚠️ Docling 分页导出失败，回退为整篇导出：{e}")

    try:
        yield 1, document.export_to_markdown()
    except Exception as e:
        print(f"❌ Docling 导出 Markdown 失败：{e}")


def _process_with_docling(uploaded_file, owner_uuid, file_uuid, source_name):
    """Docling 处理 PDF/Word/PPT（按页切分，保留真实页码）"""
    file_ext = os.path.splitext(uploaded_file.name)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    try:
        try:
            converter = get_docling_converter()
            result = converter.convert(tmp_path)
        except Exception as e:
            print(f"❌ Docling 解析失败：{e}")
            return []

        headers_to_split_on = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
        md_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on, strip_headers=False)
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)

        results = []
        for page_no, md_text in _iter_page_markdown(result):
            if not md_text or not md_text.strip():
                continue
            # 前端展示的是 page + 1，所以这里统一存 0 基页码
            zero_based_page = max(int(page_no) - 1, 0)
            for chunk in md_splitter.split_text(md_text):
                chunk.metadata.update({"source": source_name, "page": zero_based_page,
                                       "owner_uuid": owner_uuid, "file_uuid": file_uuid})
                clean_text = chunk.page_content.replace("<!-- image -->", "").strip()
                if not clean_text or len(clean_text) < 10:
                    continue
                if len(chunk.page_content) > 1500:
                    for sub in text_splitter.split_documents([chunk]):
                        sub.metadata.update(chunk.metadata)
                        results.append(sub)
                else:
                    results.append(chunk)
        return results
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def process_uploaded_document_with_parent_child(uploaded_file, owner_uuid, file_uuid, source_name):
    """统一入口：返回 {"parents": [...], "children": [...]}"""
    file_ext = os.path.splitext(uploaded_file.name)[1].lower()

    # Excel 走专用结构化路径
    if file_ext in [".xlsx", ".xls"]:
        structured = _process_excel(uploaded_file, owner_uuid, file_uuid, source_name)
        return {
            "parents": [item["parent"] for item in structured],
            "children": [item["child"] for item in structured],
        }

    # 其他走 Docling
    base_splits = _process_with_docling(uploaded_file, owner_uuid, file_uuid, source_name)
    parents, children = [], []
    for split in base_splits:
        if len(split.page_content) <= 1500:
            pid = str(uuid.uuid4())
            split.metadata["parent_id"] = pid
            parents.append(split)
            children.append(split)
        else:
            pid = str(uuid.uuid4())
            ps = Document(page_content=split.page_content, metadata=split.metadata.copy())
            ps.metadata["parent_id"] = pid
            parents.append(ps)
            for sub in RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP).split_documents([split]):
                sub.metadata["parent_id"] = pid
                children.append(sub)

    return {"parents": parents, "children": children}