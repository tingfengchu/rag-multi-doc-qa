"""原件留存与找回：文件管理 / 下载的**存储层**。

为什么需要这一层（先是事实，再是设计）：

1. 改造前**没有任何地方**保存上传的原件。``services/pdf_service`` 把字节写进
   ``tempfile.NamedTemporaryFile``，解析完在 ``finally`` 里 `os.unlink`，
   ``file_dedup`` 只剩元数据 —— 所以"下载原文"当时没有落脚点，不是"少写了个端点"。
2. 交付环境里能下载到的原件其实**已经在磁盘上**：那 3 个公共库文档的原始文件就放在
   项目根目录（``SAGE AI 智能问答助手.pdf`` / ``1790569531500.pdf`` /
   ``自动驾驶算法与嵌入式具身智能运控.pdf``）。所以找回分两步：
   先找本项目留存件 → 再按**文件名精确匹配**去兜底目录找。

命名策略：留存件用 ``<file_uuid><ext>``，**不用原名**。

* 同名文件（不同库、不同版本）不会互相覆盖；
* 原名里的 ``../``、``C:\\`` 之类根本进不了路径（文件名只留在 DB 里，用于展示与
  ``Content-Disposition``），路径穿越在构造上就不成立；
* 扩展名保留，浏览器/mimetypes 才能给出正确的 MIME。

安全性：``filename`` 参数一律来自 ``file_dedup.file_name``（入库时记下的**原始文件名**），
不是下载请求里的客户端输入 —— 下载接口只接受 ``file_uuid``。也就是说"按文件名去磁盘上找"
不构成"任意读文件"：能被读到的名字必然先是一条入库记录。下面仍加 ``_is_bare_name``
作为纵深防御（万一将来有人把它接到别处的输入上）。
"""
import os
from typing import List, Optional

from config import settings
from utils.logger import logger


def store_root() -> str:
    """留存目录（可能还不存在，写入时再创建）。"""
    return settings.file_store_root


def legacy_dirs() -> List[str]:
    """历史原件兜底目录（默认只有项目根目录）。"""
    return settings.file_legacy_dir_list


def _is_bare_name(name: str) -> bool:
    """是不是"纯文件名"：没有目录分隔符、不是空、不是 ``.`` / ``..``。

    只要有一项不满足就拒绝使用它去拼路径 —— 这层校验的收益不是"现在安全"
    （现在的输入来自 DB），而是"以后接别的输入时不会一不小心变成任意文件读取"。
    """
    if not name or name in (".", ".."):
        return False
    if name != os.path.basename(name):
        return False
    return not any(sep in name for sep in ("/", "\\", "\x00"))


def _ext(filename: str) -> str:
    """取扩展名（一律小写、限长）：留存件的后缀，用于 MIME 推断与查看器识别。

    ``.tar.gz`` 这类多段后缀只取最后一段即可 —— 这里存的是用户上传的**单个文档**，
    不需要归档后缀语义。限长是防止"文件名带一长串点"把留存件名撑爆。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if len(ext) <= 12 else ""


def stored_path(file_uuid: str, filename: str) -> str:
    """留存件的目标路径（**不**校验存在性）。"""
    return os.path.join(store_root(), f"{file_uuid}{_ext(filename)}")


def save_original(file_uuid: str, filename: str, content: bytes) -> str:
    """把上传的原件落到留存目录，返回落盘路径。

    先写 ``.part`` 再 ``os.replace``：解析/入库失败后重试时不会留下"写了一半的原件"
    （半截文件照样有正确的扩展名，肉眼根本看不出问题）。
    """
    if not _is_bare_name(file_uuid):
        raise ValueError(f"file_uuid 非法：{file_uuid!r}")

    root = store_root()
    os.makedirs(root, exist_ok=True)
    path = stored_path(file_uuid, filename)
    tmp = f"{path}.part"
    with open(tmp, "wb") as fh:
        fh.write(content)
    os.replace(tmp, path)
    logger.info(
        "file_original_saved",
        extra={"file_uuid": file_uuid, "ext": _ext(filename), "bytes": len(content)},
    )
    return path


def _find_legacy(filename: str) -> Optional[str]:
    """按**精确文件名**在兜底目录里找原件。

    多个目录里同名文件（歧义）时**返回 None 而不是随便挑一个**：挑错就是"下载到的
    不是自己看的那份"，而这种错误没人会当场发现。让下载接口如实回 404，
    日志里有 ``file_original_ambiguous`` 可查。
    """
    if not _is_bare_name(filename):
        return None

    matches: List[str] = []
    for directory in legacy_dirs():
        candidate = os.path.join(directory, filename)
        try:
            if os.path.isfile(candidate):
                matches.append(candidate)
        except OSError as e:  # 目录不可读 / 路径过长：跳过这个目录，别让整次下载 500
            logger.warning("file_legacy_scan_failed", extra={"dir": directory, "err": str(e)})

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning(
            "file_original_ambiguous",
            extra={"file_name": filename, "count": len(matches), "dirs": legacy_dirs()},
        )
    return None


def locate(file_uuid: str, filename: str) -> Optional[str]:
    """按 ``file_uuid`` 找回原件：留存件优先，其次兜底目录里的同名原件。

    返回 ``None`` = 找不到（下载接口据此回 404，并在文案里说清"可能是改造前上传的"）。
    """
    if _is_bare_name(file_uuid):
        path = stored_path(file_uuid, filename)
        if os.path.isfile(path):
            return path
    return _find_legacy(filename)


def readable_size(path: str) -> Optional[int]:
    """``path`` 上**真实可读**的字节数；打不开 / 已被删时返回 ``None``。

    为什么不能拿 ``os.path.getsize``（即 ``os.stat().st_size``）当"要发给客户端的长度"：
    这台机器上 ``.xlsx`` 这类文件的 stat 大小会比真实内容**多报**一大截。
    2026-10 排查「上传 xlsx 成功、点下载浏览器却说无法下载」时实测（同一份 12898 字节的 xlsx）：

    * 写进 ``a.bin``  → ``getsize = 12898``，读出来 12898（一致）；
    * 写进 ``a.xlsx`` → ``getsize = 20480``，读出来还是 12898（**多报 7582**）；
    * 换成 ``%TEMP%`` 目录结果一样 —— 与目录无关，与**扩展名**有关；
    * 另一次探针里写 304 字节的 ``.xlsx``，``stat`` 直接报 8192（成因未追）——
      也就是说"从 stat 取长度"在这里根本不可靠，跟文件多大无关。

    而下载接口原先用 ``FileResponse``，它内部正是拿 ``os.stat`` 当 ``Content-Length``
    （见 ``api/routers/files.py:_original_response``）：响应声明 20480、实际只发得出
    12898 字节 → 浏览器读到一半连接断掉，弹「失败 - 无法下载」。PDF 的 stat 与真实长度
    恰好一致，所以同一个接口下 PDF 正常 —— 这就是"只有 Excel 下不了"的全部原因。

    于是本层的口径定死成两句话：**"文件在不在"看 stat，"有多少字节"看读出来的长度**。
    这里用 ``seek(0, SEEK_END) + tell()``：实测它与 ``read()`` 的总字节数一致（12898），
    而 ``getsize()`` 是 20480；相比"把整份原件读进内存再数长度"，它只多一次 open。
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            return fh.tell()
    except OSError:
        return None


def size_of(file_uuid: str, filename: str) -> Optional[int]:
    """原件大小（= **真实可读字节数**）；找不到、或读不到时返回 ``None``。

    文件管理面板用它一次拿到"可下载性 + 大小"两件事（``None`` 即原件不在），
    省掉"先 ``exists()`` 再 ``getsize()``"那种同一个文件被查两遍的写法。
    长度口径见 ``readable_size``：**不读 stat**，否则面板会显示一个"下不下来的大小"
    （本机 xlsx 实测 20480，真实 12898）。
    """
    path = locate(file_uuid, filename)
    if not path:
        return None
    return readable_size(path)


def remove_original(file_uuid: str, filename: str) -> bool:
    """删掉**本项目留存**的原件（入库失败时清理），返回是否真的删了。

    只动 ``store_root()`` 下的留存件，**绝不碰兜底目录里的历史原件**：
    那些是用户自己的文件（项目根目录里放的就是交付资料），
    误删的代价远大于"少清理一个不可达的留存件"。
    """
    if not _is_bare_name(file_uuid):
        return False
    path = stored_path(file_uuid, filename)
    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("file_original_removed", extra={"file_uuid": file_uuid})
            return True
    except OSError as e:
        logger.warning(
            "file_original_remove_failed", extra={"err": str(e), "file_uuid": file_uuid}
        )
    return False
