from typing import Optional, Dict, List
from db import get_conn
from utils.logger import logger

def check_duplicate(file_hash: str, scope: str) -> Optional[Dict]:
    """检查是否已经有 success 状态的同文件，防止重复入库"""
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT id, source, status FROM file_dedup "
                "WHERE file_hash = %s AND scope = %s AND status = 'success'",
                (file_hash, scope),
            )
            return cur.fetchone()

def create_pending(file_hash: str, scope: str, file_name: str, source: str, owner_uuid: str, file_uuid: str,
                   version: Optional[int] = None) -> int:
    """插入一条 pending 记录，返回记录 id。如果存在同 hash 和 scope 的 failed/pending 记录，先删除它们。

    ``version``（模块 5）：同名文件在该库里的第几个版本。
    REST 上传路径会**显式传**它——那里必须先知道版本号，才能决定"要不要按内容哈希拒收"
    （见 ``api/routers/files.py``）；不传（``None``，Streamlit 版 ``app.py`` 的老调用）
    时在这里按 ``latest_version`` 兜底算一次 = 同名最新版本 + 1，保证"同一个
    (库, 文件名) 下版本号不重复"这条不变量与入口无关。
    """
    if version is None:
        current = latest_version(scope, file_name)
        version = int((current or {}).get("version") or 0) + 1
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. 先清理掉旧的、未成功的记录（failed 或 pending），保证可以重试
            cur.execute(
                "DELETE FROM file_dedup WHERE file_hash = %s AND scope = %s AND status IN ('pending', 'failed')",
                (file_hash, scope)
            )
            # 2. 再插入新的 pending 记录
            cur.execute(
                "INSERT INTO file_dedup "
                "(file_hash, scope, file_name, source, owner_uuid, file_uuid, status, version) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)",
                (file_hash, scope, file_name, source, owner_uuid, file_uuid, version),
            )
            return cur.lastrowid


def latest_version(scope: str, file_name: str) -> Optional[Dict]:
    """取该库里**同名文件**的最新成功版本（``{"file_uuid", "version"}``），没有则 None。

    这是模块 5 的判定入口，只做查询、不改状态：上传流程要在**动任何状态之前**决定
    "这是一份新文件，还是某个已有文件的新版本"（旧版本直到新版本入库成功才会被归档，
    中途失败时用户仍能看到原来那份，不会出现"传坏了、旧版也没了"）。
    """
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT file_uuid, version FROM file_dedup "
                "WHERE scope = %s AND file_name = %s AND status = 'success' "
                "ORDER BY version DESC, id DESC LIMIT 1",
                (scope, file_name),
            )
            return cur.fetchone()


def archive_versions(scope: str, file_name: str, keep_file_uuid: str) -> int:
    """把同名旧版本标成"历史版本"（``is_archived = 1``），返回被归档的行数。

    * 只动 ``status = 'success'`` 的行：pending / failed 是残渣，归档它们没有意义
      （``create_pending`` 本来就会把它们删掉）；
    * ``keep_file_uuid`` 是**本次刚入库成功**的那一行：用 file_uuid 排除比用 ``version < N``
      稳——将来若出现手动改过版本的记录，也不会把新版本自己归档掉；
    * 归档**不删数据**：旧版本的 ES 块、向量、留存原件都还在，面板里打开「显示历史版本」
      就能看、能下载（用户要的正是"上一版长什么样"）。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE file_dedup SET is_archived = 1 "
                "WHERE scope = %s AND file_name = %s AND status = 'success' AND file_uuid <> %s",
                (scope, file_name, keep_file_uuid),
            )
            archived = cur.rowcount
    logger.info(
        "file_versions_archived",
        extra={"file_uuid": keep_file_uuid, "scope": scope, "archived": archived},
    )
    return archived

def mark_success(file_uuid: str, scope: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE file_dedup SET status = 'success' "
                "WHERE file_uuid = %s AND scope = %s",
                (file_uuid, scope),
            )
    logger.info("file_state_success", extra={"file_uuid": file_uuid, "scope": scope})

def mark_failed(file_uuid: str, scope: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE file_dedup SET status = 'failed' "
                "WHERE file_uuid = %s AND scope = %s",
                (file_uuid, scope),
            )
    logger.warning("file_state_failed", extra={"file_uuid": file_uuid, "scope": scope})

def delete_record(file_uuid: str, scope: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM file_dedup WHERE file_uuid = %s AND scope = %s",
                (file_uuid, scope),
            )
    logger.info("file_state_deleted", extra={"file_uuid": file_uuid, "scope": scope})

def list_success_files(scope: str, include_archived: bool = False) -> List[Dict]:
    """列出某库的成功文件。

    ``include_archived``（模块 5，默认 ``False``）：是否连历史版本一起列。

    * 默认 **不列**：这个函数同时喂着侧栏的"文件级过滤"下拉——把已被同名新版取代的
      旧文件也放进去，下拉里就会出现两个一模一样的文件名（uuid 不同），
      用户选中旧版后检索到的还是旧内容，看起来像"新上传的没生效"；
    * ``True`` 时连历史版本一起列，且带上 ``version`` / ``is_archived``：
      删除路径（``DELETE /files/{uuid}``）**必须**用它，否则归档版本变成了
      "普通用户点不到、接口也删不掉"的幽灵数据。
    """
    condition = "" if include_archived else " AND is_archived = 0"
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT file_name, source, file_uuid, version, is_archived FROM file_dedup "
                f"WHERE scope = %s AND status = 'success'{condition}",
                (scope,),
            )
            return cur.fetchall()


def get_file(file_uuid: str) -> Optional[Dict]:
    """按 ``file_uuid`` 取一条成功入库的记录（**不按 scope 过滤**）。

    为什么下载接口要"先跨库取到记录、再判权限"，而不是把 scope 拼进 WHERE：
    拼进去的话，"别人的私人库文件"会因为查不到而变成 404「文件不存在」——
    可那个文件刚刚还在界面上（引用弹窗里）出现过，用户只会以为系统坏了；
    分开写才能如实回 403「无下载权限」，也才和「文件已被删除」区分得开。
    """
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT file_uuid, file_name, scope, owner_uuid, source, "
                "       created_at, updated_at "
                "FROM file_dedup WHERE file_uuid = %s AND status = 'success'",
                (file_uuid,),
            )
            return cur.fetchone()


def _usernames_for(uuids: List[str]) -> Dict[str, str]:
    """按 user_uuid 批量取用户名（``{uuid: username}``）。

    为什么不 ``LEFT JOIN users``：**两个表的排序规则不一样**——
    ``file_dedup`` 是运行期建的（MySQL 8 默认 ``utf8mb4_0900_ai_ci``），
    而 ``users`` 来自 ``migrations/001_init.sql``（``utf8mb4_unicode_ci``），
    一 JOIN 就报::

        1267 (HY000): Illegal mix of collations (utf8mb4_0900_ai_ci,IMPLICIT)
                      and (utf8mb4_unicode_ci,IMPLICIT) for operation '='

    （2026-10-13 实测：文件管理面板整个 500，页面上只显示"文件列表加载失败"。）
    这里改成"两条简单查询 + Python 里合并"：参数是可强转的（COERCIBLE），
    比较时不会再有排序规则冲突，也就不必把某个排序规则硬编码进 SQL ——
    换个环境（不同 MySQL 版本/表的建法不同）也不会再翻车。
    """
    if not uuids:
        return {}
    placeholders = ", ".join(["%s"] * len(uuids))
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                f"SELECT user_uuid, username FROM users WHERE user_uuid IN ({placeholders})",
                tuple(uuids),
            )
            return {row["user_uuid"]: row["username"] for row in cur.fetchall()}


def names_for_uuids(uuids: List[str]) -> Dict[str, str]:
    """按 file_uuid 批量取文件名（``{uuid: file_name}``）——**不做 JOIN**。

    运营看板（模块 6）要把它统计出来的 file_uuid 翻成人能看的文件名。为什么不像
    ``_usernames_for`` 那样直接 JOIN：本表的排序规则在交付机上是运行期建的
    ``utf8mb4_0900_ai_ci``（见 ``_usernames_for`` 里那次 1267 事故），而
    ``retrieval_log`` / ``feedback`` 来自 migration 的 ``utf8mb4_unicode_ci``；
    跨排序规则的 JOIN 会在**统计接口**上再翻一次同样的车。两条简单查询 + Python 合并
    没有任何这类风险。

    查不到（文件已被删除）就不返回那个键，调用方按空串/占位文案处理——删掉的文件
    仍然要留在统计里，它确实被检索过。
    """
    if not uuids:
        return {}
    placeholders = ", ".join(["%s"] * len(uuids))
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                f"SELECT file_uuid, file_name FROM file_dedup WHERE file_uuid IN ({placeholders})",
                tuple(uuids),
            )
            return {row["file_uuid"]: row["file_name"] for row in cur.fetchall()}


def list_library_files(scopes: List[str], include_archived: bool = False) -> List[Dict]:
    """文件管理面板的数据：若干库里全部成功入库的文件 + 上传人用户名。

    * 上传人用户名**单独查一次**再合并（原因见 ``_usernames_for``：跨表排序规则冲突）；
      账号已被清理时用户名缺失，调用方按空串处理——**不能**因此把文件整条丢掉，
      那才是真的丢数据；
    * ``version`` / ``is_archived``（模块 5）是**库里的真实列**了：改造前这里没有 version 列，
      版本号靠调用方按返回顺序累加，一旦改排序方式就会跟着乱跳（见 migration 004 的说明）；
    * ``ORDER BY scope, file_name, version DESC, id``：把同名文件的各个版本聚在一起、
      新版在前——面板列表要的正是这个顺序（「显示历史版本」打开时，v2 紧跟着它的 v1）。
      顺序必须是确定的，否则同一份数据两次刷新的行序都会变；
    * ``include_archived=False``（默认）时只列最新版本：归档版本平时不该出现，
      否则"库里到底有几个文件"这件事会随历史遗留数量一起膨胀；
    * 占位符按 ``scopes`` 长度动态拼：``both`` 展开成两个库后这里仍然只有一条 SQL。
    """
    if not scopes:
        return []
    placeholders = ", ".join(["%s"] * len(scopes))
    condition = "" if include_archived else " AND is_archived = 0"
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT file_uuid, file_name, scope, owner_uuid, source, "
                "       version, is_archived, created_at, updated_at "
                "FROM file_dedup "
                f"WHERE scope IN ({placeholders}) AND status = 'success'{condition} "
                "ORDER BY scope, file_name, version DESC, id",
                tuple(scopes),
            )
            rows = cur.fetchall()

    usernames = _usernames_for(sorted({row["owner_uuid"] for row in rows}))
    for row in rows:
        row["owner_username"] = usernames.get(row["owner_uuid"], "")
    return rows

