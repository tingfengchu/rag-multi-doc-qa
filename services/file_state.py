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

def create_pending(file_hash: str, scope: str, file_name: str, source: str, owner_uuid: str, file_uuid: str) -> int:
    """插入一条 pending 记录，返回记录 id。如果存在同 hash 和 scope 的 failed/pending 记录，先删除它们。"""
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
                "(file_hash, scope, file_name, source, owner_uuid, file_uuid, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
                (file_hash, scope, file_name, source, owner_uuid, file_uuid),
            )
            return cur.lastrowid

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

def list_success_files(scope: str) -> List[Dict]:
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT file_name, source, file_uuid FROM file_dedup "
                "WHERE scope = %s AND status = 'success'",
                (scope,),
            )
            return cur.fetchall()