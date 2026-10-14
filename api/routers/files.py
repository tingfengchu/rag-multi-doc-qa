"""文件路由：列表 / 上传（同步解析入库）/ 下载 / 预览 / 删除。

上传流程是 ``app.py:120-191`` 的 1:1 迁移，**顺序不能动**：
去重 → pending → 解析 → Chroma → ES（父块+子块）→ success → refresh。
少任何一步都会留下"孤儿向量"或"刚上传完搜不到"的顽疾。

（Phase 1 刻意保持**同步**：Docling 解析 10~60s，前端转圈等待即可；
改成后台任务需要额外的任务表与状态查询，留到 Phase 2。）
"""
import hashlib
import os
from typing import Dict, List, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from api import errors
from api.deps import (
    PRIVATE_PREFIX,
    PUBLIC_SCOPE,
    describe_scope,
    get_current_user,
    private_scope,
    resolve_scope,
)
from api.schemas import FileOut, LibraryFileOut, MessageOut, UploadOut
from config import settings
from services import es_service, file_state, file_store, pdf_service, redis_service, vector_service
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


def _rollback(scope: str, source: str, file_uuid: str, filename: str) -> None:
    """入库失败后的清理：向量 + ES + 留存原件都删掉，避免留下搜得到却打不开的孤儿块。

    留存原件也要清（``file_store.remove_original``）：入库失败时 DB 记录是 ``failed``，
    下载接口按 ``status='success'`` 查不到它 —— 那份留存件永远不可达，
    留着只是占空间。注意它**只删本项目留存件**，不会碰兜底目录里的历史原件。
    """
    try:
        vectorstore = vector_service.get_vectorstore(scope)
        vector_service.delete_by_source(vectorstore, source)
        es_service.delete_by_file_uuid(file_uuid, scope)
        file_store.remove_original(file_uuid, filename)
    except Exception as cleanup_err:
        logger.warning(
            "cleanup_failed", extra={"err": str(cleanup_err), "file_uuid": file_uuid}
        )



@router.get("", response_model=List[FileOut], summary="列出某库中已入库的文件")
def list_files(
    scope: str = "public",
    include_archived: bool = Query(
        False, description="是否连历史版本一起列（默认只列每个文件的最新版本）"
    ),
    user=Depends(get_current_user),
) -> List[FileOut]:
    """列出**某个具体库**里已入库的文件。

    ``include_archived``（模块 5）默认 ``False``：只回每个文件的最新版本。
    为什么默认不带历史版本——这个端点还喂着侧栏的「文件级过滤」下拉，把已被同名新版
    取代的旧文件也列进去，下拉里就会出现两个一模一样的文件名（uuid 不同），
    用户选中旧版后检索到的还是旧内容，看起来像"新上传的没生效"。
    """
    resolved = resolve_scope(scope, user)
    return [
        FileOut(**row)
        for row in file_state.list_success_files(resolved, include_archived=include_archived)
    ]


def _can_delete(scope: str, user: Dict[str, str]) -> bool:
    """这一行**能不能删**：拿 ``resolve_scope(..., write=True)`` 预演一次删除授权。

    为什么用"预演"而不是在面板层（或这里）再写一遍 ``role == 'admin' or owner == 我``：
    删除的授权规则只能有一份。第二份副本的失效形态是"界面给了删除按钮、点下去才 403"
    ——更难发现的是反过来那种：明明有权限却不给入口，用户只会以为功能不存在。
    这里与 ``DELETE /files/{uuid}`` 调的是**同一个** ``resolve_scope``：它放行 → 按钮可见，
    它拒绝 → 按钮不出现，两者不可能漂移。纯函数调用（不查库、不连 ES），没有额外开销。

    未知 / 非法的 scope 一律按"不能删"处理（fail-closed）：脏数据不该换来一个能点的按钮。
    """
    try:
        resolve_scope(scope, user, write=True)
    except HTTPException:
        return False
    return True


def _library_rows(
    scopes: List[str], user: Dict[str, str], include_archived: bool = False
) -> List[LibraryFileOut]:
    """把 DB 行翻成面板项（版本号 / 归档标记 / 原件可下载性 / 能否删除都在这里对齐）。

    ``user`` 只用于算 ``can_delete``（公共库看角色、私人库看是不是本人）——
    库的范围仍由调用方按会话决定，这个参数**不会**扩大任何人能看到的内容。

    版本号（模块 5）**直接取 ``file_dedup.version``**，不再按返回顺序累加：
    累加只在"列表恰好按 created_at 排序"时才对，而面板要的是"同名文件聚在一起、
    新版在前"（见 ``file_state.list_library_files`` 的 ORDER BY）——用累加的话，
    排序方式一改，界面上就会冒出"同一个文件两个 v1"这种自相矛盾的行。
    """
    items: List[LibraryFileOut] = []
    for row in file_state.list_library_files(scopes, include_archived=include_archived):
        # 一次拿到"原件在不在 + 多大"：None = 原件不在了（改造前上传的文件就是这种），
        # 面板据此把下载按钮置灰并说明原因，而不是让用户点了才吃 404。
        size = file_store.size_of(row["file_uuid"], row["file_name"])
        items.append(
            LibraryFileOut(
                file_uuid=row["file_uuid"],
                file_name=row["file_name"],
                scope=row["scope"],
                scope_label=describe_scope(row["scope"]),
                owner_uuid=row["owner_uuid"],
                owner_username=row.get("owner_username") or "",
                # ``or 1``：老库（还没跑 migration 004）没有这两列时，行里就没有这些键，
                # 此时按"每个文件都是 v1、都不是历史版本"显示——与改造前一致，不报错。
                version=int(row.get("version") or 1),
                archived=bool(row.get("is_archived")),
                updated_at=row.get("updated_at") or row.get("created_at"),
                size_bytes=size,
                downloadable=size is not None,
                # 「删除」按钮的可见性由后端算（见 ``_can_delete``）：公共库按角色、
                # 私人库按"是不是自己的库"，前端不做角色判断（一份规则只留一处）。
                can_delete=_can_delete(row["scope"], user),
            )
        )
    return items


@router.get(
    "/library",
    response_model=List[LibraryFileOut],
    summary="文件管理：公共库 + 我的私人库（含上传人 / 版本 / 原件可下载性）",
)
def list_library(
    include_archived: bool = Query(
        False, description="是否连历史版本一起列（面板的「显示历史版本」开关）"
    ),
    user=Depends(get_current_user),
) -> List[LibraryFileOut]:
    """文件管理面板的列表（**只读**，按角色给出"我能看到的原件"）。

    为什么单独开一个端点，而不是给 ``GET /files`` 加 ``scope=both``：
    那个端点的语义是"**某一个具体库**"——上传页的"文件级过滤"下拉依赖它逐个库去拉，
    而 ``both`` 会被 ``deps.resolve_scope`` 判 400（这条约定写在 deps 的文档与
    ``tests/unit/test_scope_modes.py`` 里）。面板问的是另一个问题：
    "我现在能看到哪些原件"（公共库 + 我自己的私人库）。两件事混在一个端点上，
    将来"下拉里少了一个库"这种事故会变得很难查。

    权限：公共库登录即可读；私人库**永远只拼会话里的 uuid**（``private_scope(user)``），
    所以别人的私人库不可能出现在列表里——不是靠前端不显示，是后端根本不查。
    ``include_archived=True``（模块 5）会把历史版本也列出来并标上 ``archived``：
    权限规则与最新版本**完全一致**，历史版本不是另一类数据。
    """
    scopes = [PUBLIC_SCOPE, private_scope(user)]
    return _library_rows(scopes, user, include_archived=include_archived)


#: 读取类入口的动作名（slug → 中文）：下载与预览**共用**同一个权限实现，
#: 只有文案与日志前缀按动作区分（见 ``_row_and_original``）。
_READ_ACTIONS = {"download": "下载", "preview": "预览"}


#: 可以 ``inline`` 打开的扩展名白名单（**预览端点专用**）。
#: blob URL 与直接导航都**继承本站 origin**：能被当页面渲染的类型（``.html`` / ``.svg`` /
#: ``.xml``）一旦内嵌打开，等于让上传者在本站执行脚本。白名单**以外一律 attachment**，
#: 前端据此走下载——预览按钮对 Word / Excel / HTML 本来也只能降级成下载，收紧没有代价。
#: 前端**不**维护第二份名单：它只看响应的 ``Content-Disposition``（一份规则只留一处）。
_INLINE_SAFE_SUFFIXES = (
    ".pdf",
    ".txt",
    ".md",
    ".log",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
)


def _row_and_original(file_uuid: str, user, action: str) -> Tuple[Dict, str]:
    """按 ``file_uuid`` 取记录与原件路径——下载 / 预览**共用同一套权限判断**。

    抽成一个函数而不是两个端点各写一遍：预览就是把下载的 ``Content-Disposition``
    换成 ``inline``，其余完全一样；权限规则抄第二份的下场是某天只有一边被修好
    （"预览放行了、下载没放行"这种错位很难被人工发现，因为两边单独看都挺合理）。

    权限矩阵、403 与 404 的取舍、以及"文件名只来自 DB 记录"这几条写在
    ``download_file`` 的说明里——那份说明是这条链路的**唯一**口径，改规则时改那里。
    这里只保证"两个入口走的是同一份实现"。
    """
    label = _READ_ACTIONS[action]
    row = file_state.get_file(file_uuid)
    if not row:
        raise errors.not_found("文件不存在或已被删除")

    scope = row["scope"]
    if scope.startswith(PRIVATE_PREFIX) and scope != private_scope(user):
        logger.warning(
            f"file_{action}_denied",
            extra={
                "file_uuid": file_uuid,
                "scope": scope,
                "user_uuid": user.get("user_uuid"),
            },
        )
        raise errors.forbidden(f"无{label}权限，请联系管理员")

    path = file_store.locate(file_uuid, row["file_name"])
    if not path:
        # 找不到原件既不是"无权限"、也不是"记录不存在"：如实说清原因，
        # 否则用户只会反复点（改造前上传的文件确实没有留存件）。
        logger.warning(f"file_{action}_missing_original", extra={"file_uuid": file_uuid})
        raise errors.not_found(
            "未找到该文件的原件（可能是本次改造前上传的历史文件），请联系管理员重新上传"
        )
    return row, path


def _original_response(row: Dict, path: str, disposition: str) -> FileResponse:
    """把原件包成 ``FileResponse``，但 ``Content-Length`` 取**真实可读字节数**。

    为什么不交给 ``FileResponse`` 自己写这个头：它内部取 ``os.stat().st_size``
    （``starlette/responses.py`` 的 ``set_stat_headers``），而**本机上 ``.xlsx`` 的 stat
    比真实内容多报**（实测 ``stat = 20480`` / 真实 12898，见 ``file_store.readable_size``）。
    于是响应声明 20480、实际只发得出 12898 字节 → 浏览器收尾时连接断掉，
    下载栏里弹「失败 - 无法下载」。这就是"Excel 下不了、PDF 正常"的根因
    （PDF 的 stat 与真实长度恰好一致），与权限、MIME、二进制读取都无关：
    存留件本身是逐字节完好的（MD5 与源文件一致）。

    能这样"从外面"改掉长度，是因为 Starlette 用的是
    ``headers.setdefault("content-length", …)``：**调用方传进去的值优先**，不会被 stat 盖掉。
    ``filename`` 仍交给 Starlette 走 RFC 5987（``filename*=utf-8''…``），中文名不乱码。

    下载与预览**共用**这一个构造函数：长度口径只能有一份，两边各写一遍迟早只有一边被修。
    （Range 请求仍由 Starlette 按 stat 自行计算；本项目前端一律整块取文件、从不发 Range。）
    """
    headers = {"Cache-Control": "private, no-store"}
    size = file_store.readable_size(path)
    if size is not None:
        headers["Content-Length"] = str(size)
    return FileResponse(
        path,
        filename=row["file_name"],
        content_disposition_type=disposition,
        headers=headers,
    )


@router.get("/{file_uuid}/download", summary="下载原件（公共库人人可下；私人库仅本人）")
def download_file(file_uuid: str, user=Depends(get_current_user)) -> FileResponse:
    """下载某个文件的原件。

    权限（**先取记录、再判权限**，顺序不能颠倒）：

    * 记录不存在 / 已删除 → 404「文件不存在或已被删除」；
    * ``public``                       → 任何**已登录**用户都能下载；
    * ``private:<owner>``              → 只有 ``owner == 会话里的 user_uuid``，否则 403。

    为什么私人库连管理员也不放行：能力边界与 ``deps.resolve_scope`` 一致
    （"别人的私人库"对任何角色都不开），而且管理端**看不到**别人的私人库文件列表，
    放行只会造出一条"知道 uuid 就能读别人文档"的通道。

    为什么 403 与 404 要分开（会暴露"这个 uuid 存在"）：file_uuid 是 uuid4、
    界面上只出现在**自己有权限**的引用里，猜不到；反过来把"无权"报成"不存在"
    会让用户拿着刚看到的引用去排障，越查越糊涂。

    文件名与 MIME 都取自 **DB 记录**（不是客户端输入）：下载接口只接受 ``file_uuid``，
    所以路径里不含任何用户可控的名字，不存在目录穿越。
    """
    row, path = _row_and_original(file_uuid, user, "download")

    logger.info(
        "file_downloaded",
        extra={
            "file_uuid": file_uuid,
            "scope": row["scope"],
            "user_uuid": user.get("user_uuid"),
        },
    )
    # 长度口径、filename 的 RFC 5987 编码、Cache-Control 都在 ``_original_response`` 里
    # （下载 / 预览共用同一份）；下载一律 attachment：它的职责就是让浏览器落盘。
    return _original_response(row, path, "attachment")


@router.get("/{file_uuid}/preview", summary="在线预览原件（inline 打开；权限同下载）")
def preview_file(file_uuid: str, user=Depends(get_current_user)) -> FileResponse:
    """在线预览原件：**不下载**，交给浏览器自己的查看器就地打开。

    为什么下载之外还要这一个端点（交付验收："文件和引用片段不支持在线查看"）：

    * 引用溯源弹窗给了"第 N 页"，可用户手上没有 PDF 就无从对照——"先下载、再翻到第 N 页"
      这条链路里最容易出错的一步，恰好由用户手工完成；
    * 这里用 ``Content-Disposition: inline`` 回原件，前端在**新标签页**里打开它，并在 URL 上带
      ``#page=N``（PDF 查看器会直接跳到那一页）。页码是浏览器查看器的能力，接口因此
      **不**多收一个参数——多收一个参数就多一处能被伪造的输入，而后端本来也无从校验它。

    权限、403 / 404 文案、原件留存判定**全部复用** ``_row_and_original``（与下载同一份代码）：
    能预览一份文件就等于能读到它的内容，两个入口的规则必须一致。

    哪些类型可以内嵌打开由**这里**决定（``_INLINE_SAFE_SUFFIXES`` 白名单）：白名单以外的类型
    （Word / Excel / HTML / SVG / 未知）照样回原件，只是 ``Content-Disposition`` 用
    ``attachment``，前端据此降级成下载。为什么这条边界不交给前端判：blob URL 继承本站
    origin，能内嵌 HTML / SVG 就等于让上传者在本站执行脚本——这种判断必须由可信的那一侧把住。
    """
    row, path = _row_and_original(file_uuid, user, "preview")

    logger.info(
        "file_previewed",
        extra={
            "file_uuid": file_uuid,
            "scope": row["scope"],
            "user_uuid": user.get("user_uuid"),
        },
    )
    # 能不能内嵌是**按类型**判的（与权限无关）：不在白名单里就回 attachment，
    # 前端看到 attachment 就落盘（见 client.ts:previewFile）——预览按钮对这类文件
    # 本来也只能降级成下载，所以这里收紧没有可用性损失。
    disposition = (
        "inline"
        if row["file_name"].lower().endswith(_INLINE_SAFE_SUFFIXES)
        else "attachment"
    )
    if disposition == "attachment":
        logger.info(
            "file_preview_downgraded",
            extra={"file_uuid": file_uuid, "file_name": row["file_name"]},
        )
    # 只把 ``Content-Disposition`` 换成 inline，其余（长度口径 / Cache-Control / filename）
    # 与下载**共用** ``_original_response``——这条长度口径以前两边都漏，就是 Excel 下不来的原因。
    return _original_response(row, path, disposition)



#: 图片扩展名：**暂不支持上传**（决策）。前六项与 ``_INLINE_SAFE_SUFFIXES`` 里的图片项
#: 同源——那是"本项目认过的图片类型"；两份名单各写各的，迟早出现"预览放行过的类型、
#: 上传却漏进来"这种错位。后六项是本项目从没认过的常见图片格式，一并拒掉。
_IMAGE_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".tif",
    ".tiff",
    ".ico",
    ".heic",
    ".avif",
)

#: 图片上传被拒时的统一文案。用常量而不是两处各写一遍：前端把 ``detail`` 原样展示，
#: 文案漂移（"暂不支持图片" / "图片暂不支持上传"）会让用户以为是两回事。
IMAGE_UNSUPPORTED_MESSAGE = "图片格式暂不支持上传"


def _reject_image_upload(filename: str, content_type: str | None) -> None:
    """图片一律 400：**决策是暂不支持图片**，不让它走到解析入库那一步。

    为什么不"先收下，让它自己失败"：图片进 Docling 提不出文本，用户等几十秒后收到
    一句"入库失败：未提取到任何有效文本"，看着像系统坏了。而且那份失败发生在留存原件与
    写 ``pending`` 记录**之后**——每误传一次就在 ``file_store/`` 里攒一份没人认领的图。

    两个判据都要查（**扩展名** + **声明的 MIME**），因为漏法有两种：
      * 前端选了图：浏览器把 multipart 里那个 part 的 ``Content-Type`` 定成 ``image/png``；
      * 有人脚本直传：至少文件名还带着 ``.png``。
    这里不嗅探文件头：真正的判据是"能不能解析出文本"，把图片改名 + 改 MIME 硬塞进来的
    那份交给 Docling 照旧 400，不值得为此先把整份文件读进来再看魔数。
    """
    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix in _IMAGE_SUFFIXES or (content_type or "").lower().startswith("image/"):
        raise errors.bad_request(IMAGE_UNSUPPORTED_MESSAGE)


@router.post("/upload", response_model=UploadOut, summary="上传文档（同步解析并入库）")
def upload_file(
    file: UploadFile = File(..., description="PDF/Word/PPT/Excel/HTML（暂不支持图片）"),
    scope: str = Form("public", description="public 或 private:<user_uuid>"),
    user=Depends(get_current_user),
) -> UploadOut:
    # 写权限：普通用户只能写自己的私人库（与 app.py:104-108 的能力边界一致）
    resolved = resolve_scope(scope, user, write=True)

    filename = file.filename or "unnamed"
    # 权限先判、图片次之：越权的请求连"这个库能不能写"都不该知道（403 优先于 400）。
    _reject_image_upload(filename, file.content_type)
    content = file.file.read()
    if not content:
        raise errors.bad_request("文件内容为空，请重新选择文件")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise errors.too_large(
            f"文件大小不能超过 {settings.max_upload_mb}MB，请压缩后再上传"
        )

    file_hash = hashlib.md5(content).hexdigest()

    # 内容级去重**先于**版本判断（模块 5 + 交付验收修正）：
    # 同一个库里已经有一条 **MD5 相同**的成功记录 → 直接说清"已入库，无需重复上传"，
    # 既不新建版本、也不归档旧版。
    #
    # 为什么判据是 MD5 而不是文件名（同名 + 同内容也去重）：那种情况是用户把**同一份文件**
    # 又传了一遍（误操作，或没看到上一次的提示）。记成 v2 会带来两个坏结果——
    #   ① "版本"这个信息当场失去意义：两版内容一模一样，面板上却并排显示 v1 / v2；
    #   ② 旧版会被 archive_versions 归档，用户看到的是"我的文件变成历史版本了"，
    #      而他真正想要的（确认文件在不在库里）一个都没解决。
    # 只有"同名 + 内容不同"才是真的换了一版（手册改一版、简历换一版），那时才 version + 1。
    #
    # 顺序放在留存原件之前：重复上传不该在 file_store/ 里再落一份没人认领的件。
    if file_state.check_duplicate(file_hash, resolved):
        logger.info(
            "upload_duplicate",
            extra={"scope": resolved, "file_name": filename, "file_hash": file_hash},
        )
        return UploadOut(message=f"文件 {filename} 已入库，无需重复上传", duplicate=True)

    # 同名（同库）但内容**不同** = 新版本：旧版本先不动——等新版本真的入库成功再归档
    # （见下面 mark_success 之后的 archive_versions），中途失败时用户手上那份还在。
    predecessor = file_state.latest_version(resolved, filename)
    version = int((predecessor or {}).get("version") or 0) + 1

    file_uuid = new_uuid()
    source = build_source(user["user_uuid"], file_uuid, filename)

    # 留存原件（文件管理 / 下载的落点）。**必须放在入库之前**：
    # 现在失败只是"这次上传没开始"，不会留下"入库了、却永远下不到原件"的半成品；
    # 放到 ``mark_success`` 之后，一次磁盘写失败就会制造一条下载按钮点不动的记录。
    try:
        file_store.save_original(file_uuid, filename, content)
    except OSError as e:
        logger.error(
            "file_original_save_failed", extra={"err": str(e), "file_uuid": file_uuid}
        )
        raise errors.bad_request(f"原件留存失败（请检查磁盘空间）：{e}") from e

    file_state.create_pending(
        file_hash, resolved, filename, source, user["user_uuid"], file_uuid, version
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
        # 新版本已就位 → 把同名旧版本转成"历史版本"（模块 5）。
        # 顺序不能颠倒：先归档再入库，一旦入库失败就会留下"旧版已归档、新版不存在"的
        # 空档（用户看到自己刚传的东西消失了，而那份旧的其实还在库里）。归档失败会抛进
        # 下面的 except → 走 rollback，旧版本保持原样，用户重试即可。
        file_state.archive_versions(resolved, filename, keep_file_uuid=file_uuid)
        # 强制刷新 ES：否则"刚上传完立刻提问"可能搜不到新块
        es_service.refresh_index()
    except Exception as e:
        logger.error("upload_failed", extra={"err": str(e), "file_uuid": file_uuid})
        file_state.mark_failed(file_uuid, resolved)
        _rollback(resolved, source, file_uuid, filename)
        raise errors.bad_request(f"入库失败：{e}") from e

    logger.info(
        "upload_ok",
        extra={
            "file_uuid": file_uuid,
            "scope": resolved,
            "parents": len(parents),
            "children": len(children),
            "version": version,
        },
    )
    # 版本提示写进 message：上传同名文件是**用户主动做的替换动作**，只回一句"已成功添加"
    # 会让人怀疑旧版本是不是被覆盖掉了（其实它变成了可查看的历史版本）。
    message = f"已成功添加到 {resolved}（父块 {len(parents)} / 子块 {len(children)}）"
    if version > 1:
        message = (
            f"已作为第 {version} 版添加到 {resolved}；上一版已转为历史版本"
            f"（「文件管理」→「显示历史版本」可查看或下载）"
            f"（父块 {len(parents)} / 子块 {len(children)}）"
        )
    return UploadOut(message=message, file_uuid=file_uuid, version=version)


@router.delete(
    "/{file_uuid}", response_model=MessageOut, summary="删除文件（缓存 + ES + 向量 + 状态记录 + 原件）"
)
def delete_file(
    file_uuid: str, scope: str = "public", user=Depends(get_current_user)
) -> MessageOut:
    """删除某个文件。

    删除顺序**刻意与 Streamlit 版不同**（app.py:209-212 是先向量、再记录、最后 ES）：
    这里改成 ES → Chroma → 状态记录。原因是"状态记录"是列表的依据，
    把它放在最后删，任何前序步骤失败时文件仍在列表里、用户可重试；
    反过来先删记录再删 ES，一旦 ES 报错就会留下**搜得到、却怎么都删不掉**的孤儿块。

    2026-10-14 起多一步：**先让语义缓存失效**
    （``redis_service.invalidate_cache_by_file_uuid``），顺序理由见下面 ``target = rows[0]``
    前的注释 —— 它挡的正是"删除文件后命中旧缓存 → 引用 404"那次事故。
    """
    resolved = resolve_scope(scope, user, write=True)

    # include_archived=True（模块 5）：历史版本**也**要能删。
    # 不带它的话，被同名新版取代的旧记录会在面板里可见（打开「显示历史版本」）、
    # 下载按钮也点得动，却怎么也删不掉——接口会回"文件不存在或已被删除"，
    # 用户只能去数据库里手工清。删除逻辑对版本一视同仁：它本来就按 file_uuid 干活的。
    rows = [
        f
        for f in file_state.list_success_files(resolved, include_archived=True)
        if f["file_uuid"] == file_uuid
    ]
    if not rows:
        raise errors.not_found("文件不存在或已被删除")

    target = rows[0]
    # 0) 语义缓存先行失效（2026-10-14 用户报的事故）：缓存 key 只认 scope + 版本号 + 问题哈希，
    #    看不见"库里的文件被删了"，于是那条答案会继续引用已删除文件 —— 用户再问同样的问题
    #    命中缓存 → 点引用下载 404。所以删除必须显式把这些缓存清掉。
    #
    #    为什么排在所有删除动作**之前**：Redis 不可用时这里就抛异常，ES / Chroma / 记录 /
    #    留存原件一个都还没动（文件仍在、可重试删除）；反过来放最后，一次 Redis 故障就会
    #    留下"文件已删、缓存还在"，也就是这次要修的 bug 原样复现。
    invalidated = redis_service.invalidate_cache_by_file_uuid(file_uuid)
    es_service.delete_by_file_uuid(file_uuid, resolved)
    vectorstore = vector_service.get_vectorstore(resolved)
    vector_service.delete_by_source(vectorstore, target["source"])
    file_state.delete_record(file_uuid, resolved)
    # 记录删了 → 留存原件也删：否则 file_store/ 会随着"上传又删除"无限长大，
    # 而且那些件再也无法通过接口访问（下载要先查到 success 记录）。
    # 只删本项目留存件，兜底目录里的历史原件不动（那是用户自己的文件）。
    file_store.remove_original(file_uuid, target["file_name"])
    logger.info(
        "file_deleted",
        extra={"file_uuid": file_uuid, "scope": resolved, "cache_invalidated": invalidated},
    )
    return MessageOut(message=f"已删除 {target['file_name']}")
