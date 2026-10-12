"""文件路由：列表 / 上传（同步解析入库）/ 删除。

上传流程是 ``app.py:120-191`` 的 1:1 迁移，**顺序不能动**：
去重 → pending → 解析 → Chroma → ES（父块+子块）→ success → refresh。
少任何一步都会留下"孤儿向量"或"刚上传完搜不到"的顽疾。

（Phase 1 刻意保持**同步**：Docling 解析 10~60s，前端转圈等待即可；
改成后台任务需要额外的任务表与状态查询，留到 Phase 2。）
"""
import hashlib
from typing import List

from fastapi import APIRouter, Depends, File, Form, UploadFile

from api import errors
from api.deps import get_current_user, resolve_scope
from api.schemas import FileOut, MessageOut, UploadOut
from config import settings
from services import es_service, file_state, pdf_service, vector_service
from utils.ids import build_source, new_uuid
from utils.logger import logger

router = APIRouter(prefix="/files", tags=["文件"])


class UploadedFileLike:
    """把 FastAPI 的 ``UploadFile`` 适配成 ``pdf_service`` 期待的形状（``.name`` / ``.getvalue()``）。

    为什么不直接改 ``pdf_service``：那边对接的是 **Streamlit UploadedFile** 契约，
    改它等于同时动到"正在跑的" Streamlit 版；适配器只有几行，隔离得更干净。
    """

    def __init__(self, filename: str, content: bytes):
        self.name = filename
        self.size = len(content)
        self._content = content

    def getvalue(self) -> bytes:
        return self._content


def _index_to_es(parents, children, scope: str, file_uuid: str, source: str) -> None:
    """把父子块写进 ES（父块用于回溯、子块用于 BM25）——与 app.py:158-172 逐字段一致。

    元数据字段名（``scope`` / ``page`` / ``header`` / ``file_uuid`` / ``source``）
    必须和 ``es_service`` 的 mapping 对齐，否则过滤条件会静默失效。
    """
    for parent in parents:
        es_service.index_parent_chunk(
            parent.metadata["parent_id"],
            parent.page_content,
            {
                "scope": scope,
                "page": parent.metadata.get("page", 0),
                "header": parent.metadata.get("Header 1", ""),
                "file_uuid": file_uuid,
                "source": source,
            },
        )
    for child in children:
        es_service.index_child_chunk(
            new_uuid(),
            child.metadata["parent_id"],
            child.page_content,
            {
                "scope": scope,
                "page": child.metadata.get("page", 0),
                "header": child.metadata.get("Header 1", ""),
                "file_uuid": file_uuid,
                "source": source,
            },
        )


def _rollback(scope: str, source: str, file_uuid: str) -> None:
    """入库失败后的清理：向量 + ES 都删掉，避免留下搜得到却打不开的孤儿块。"""
    try:
        vectorstore = vector_service.get_vectorstore(scope)
        vector_service.delete_by_source(vectorstore, source)
        es_service.delete_by_file_uuid(file_uuid, scope)
    except Exception as cleanup_err:
        logger.warning(
            "cleanup_failed", extra={"err": str(cleanup_err), "file_uuid": file_uuid}
        )


@router.get("", response_model=List[FileOut], summary="列出某库中已入库的文件")
def list_files(scope: str = "public", user=Depends(get_current_user)) -> List[FileOut]:
    resolved = resolve_scope(scope, user)
    return [FileOut(**row) for row in file_state.list_success_files(resolved)]


@router.post("/upload", response_model=UploadOut, summary="上传文档（同步解析并入库）")
def upload_file(
    file: UploadFile = File(..., description="PDF/Word/PPT/Excel/HTML/图片"),
    scope: str = Form("public", description="public 或 private:<user_uuid>"),
    user=Depends(get_current_user),
) -> UploadOut:
    # 写权限：普通用户只能写自己的私人库（与 app.py:104-108 的能力边界一致）
    resolved = resolve_scope(scope, user, write=True)

    filename = file.filename or "unnamed"
    content = file.file.read()
    if not content:
        raise errors.bad_request("文件内容为空，请重新选择文件")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise errors.too_large(
            f"文件大小不能超过 {settings.max_upload_mb}MB，请压缩后再上传"
        )

    file_hash = hashlib.md5(content).hexdigest()
    if file_state.check_duplicate(file_hash, resolved):
        return UploadOut(message=f"文件 {filename} 已入库，无需重复上传", duplicate=True)

    file_uuid = new_uuid()
    source = build_source(user["user_uuid"], file_uuid, filename)
    file_state.create_pending(
        file_hash, resolved, filename, source, user["user_uuid"], file_uuid
    )

    try:
        chunk_dict = pdf_service.process_uploaded_document_with_parent_child(
            UploadedFileLike(filename, content), user["user_uuid"], file_uuid, source
        )
        parents, children = chunk_dict["parents"], chunk_dict["children"]
        if not parents or not children:
            raise ValueError("Docling 未能从该文档中提取到任何有效文本。")

        vectorstore = vector_service.get_vectorstore(resolved)
        vector_service.add_documents(vectorstore, children)
        _index_to_es(parents, children, resolved, file_uuid, source)

        file_state.mark_success(file_uuid, resolved)
        # 强制刷新 ES：否则"刚上传完立刻提问"可能搜不到新块
        es_service.refresh_index()
    except Exception as e:
        logger.error("upload_failed", extra={"err": str(e), "file_uuid": file_uuid})
        file_state.mark_failed(file_uuid, resolved)
        _rollback(resolved, source, file_uuid)
        raise errors.bad_request(f"入库失败：{e}") from e

    logger.info(
        "upload_ok",
        extra={
            "file_uuid": file_uuid,
            "scope": resolved,
            "parents": len(parents),
            "children": len(children),
        },
    )
    return UploadOut(
        message=f"已成功添加到 {resolved}（父块 {len(parents)} / 子块 {len(children)}）",
        file_uuid=file_uuid,
    )


@router.delete(
    "/{file_uuid}", response_model=MessageOut, summary="删除文件（ES + 向量 + 状态记录）"
)
def delete_file(
    file_uuid: str, scope: str = "public", user=Depends(get_current_user)
) -> MessageOut:
    """删除某个文件。

    删除顺序**刻意与 Streamlit 版不同**（app.py:209-212 是先向量、再记录、最后 ES）：
    这里改成 ES → Chroma → 状态记录。原因是"状态记录"是列表的依据，
    把它放在最后删，任何前序步骤失败时文件仍在列表里、用户可重试；
    反过来先删记录再删 ES，一旦 ES 报错就会留下**搜得到、却怎么都删不掉**的孤儿块。
    """
    resolved = resolve_scope(scope, user, write=True)

    rows = [
        f for f in file_state.list_success_files(resolved) if f["file_uuid"] == file_uuid
    ]
    if not rows:
        raise errors.not_found("文件不存在或已被删除")

    target = rows[0]
    es_service.delete_by_file_uuid(file_uuid, resolved)
    vectorstore = vector_service.get_vectorstore(resolved)
    vector_service.delete_by_source(vectorstore, target["source"])
    file_state.delete_record(file_uuid, resolved)
    logger.info("file_deleted", extra={"file_uuid": file_uuid, "scope": resolved})
    return MessageOut(message=f"已删除 {target['file_name']}")
