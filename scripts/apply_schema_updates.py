"""把 ``migrations/004``、``005`` 的改动**幂等地**补到当前数据库上（模块 5 / 6）。

为什么不直接 ``mysql < migrations/004_file_versioning.sql``：
MySQL 8 **没有** ``ADD COLUMN IF NOT EXISTS``，ALTER 语句重复执行会报
``1060 Duplicate column name``。而这两个 migration 恰恰要在"已经跑着业务的老库"上补列，
被执行两次几乎是必然的（部署脚本重跑、换台机器手工补、忘了执行过没有）。这里先查
``information_schema`` 看列/索引/表在不在，**只补缺的部分**，并逐条打印它做了什么——
迁移最怕的不是失败，而是"跑完了但没人知道到底改了没有"。

用法（项目根目录下）::

    .venv\\Scripts\\python.exe scripts\\apply_schema_updates.py --dry-run   # 只看要改什么
    .venv\\Scripts\\python.exe scripts\\apply_schema_updates.py             # 真的改

⚠️ 执行前先停后端（uvicorn）：加列本身很快，但在线写入与 DDL 撞在一起时，
失败信息很难看（``Lock wait timeout``），而这类失败最容易让人误判成"代码有问题"。
"""
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，输出 emoji 会抛 UnicodeEncodeError，这里强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老 Python / 非 TTY 时忽略
    pass

from db import get_conn  # noqa: E402

#: 需要补的列：(表, 列, 补列用的 DDL)。逐条对应 migrations/*.sql 里的 ALTER。
COLUMNS = [
    (
        "file_dedup",
        "version",
        "ALTER TABLE file_dedup ADD COLUMN version INT NOT NULL DEFAULT 1 "
        "COMMENT '同名文件在同库中的第几个版本（1 基）'",
    ),
    (
        "file_dedup",
        "is_archived",
        "ALTER TABLE file_dedup ADD COLUMN is_archived TINYINT(1) NOT NULL DEFAULT 0 "
        "COMMENT '1 = 已被同名新版本取代（历史版本）'",
    ),
    (
        "feedback",
        "q_hash",
        "ALTER TABLE feedback ADD COLUMN q_hash CHAR(40) NULL "
        "COMMENT '问题归一化后的 SHA-1（👍/👎 防重键）'",
    ),
]

#: 需要补的索引：(表, 索引名, DDL)
INDEXES = [
    (
        "file_dedup",
        "idx_scope_name_version",
        "ALTER TABLE file_dedup ADD KEY idx_scope_name_version (scope, file_name, version)",
    ),
    (
        "feedback",
        "idx_user_qhash",
        "ALTER TABLE feedback ADD KEY idx_user_qhash (user_uuid, q_hash)",
    ),
]

#: 需要建的表（``CREATE TABLE IF NOT EXISTS`` 本身幂等，这里仍然先查一遍，只为了打印清楚）
TABLES = [
    (
        "retrieval_log",
        """
        CREATE TABLE IF NOT EXISTS retrieval_log (
          id                 BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
          user_uuid          CHAR(36)     NOT NULL,
          scope              VARCHAR(64)  NOT NULL,
          question           TEXT         NOT NULL,
          rewritten_question TEXT         NULL,
          hit_count          INT          NOT NULL DEFAULT 0,
          max_score          FLOAT        NULL,
          no_result          TINYINT(1)   NOT NULL DEFAULT 0,
          file_uuids         JSON         NULL,
          created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_created (created_at),
          KEY idx_no_result (no_result, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
]


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    return int(cur.fetchone()["n"]) > 0


def _index_exists(cur, table: str, index: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
        (table, index),
    )
    return int(cur.fetchone()["n"]) > 0


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return int(cur.fetchone()["n"]) > 0


def main() -> int:
    parser = argparse.ArgumentParser(description="补 004/005 的表结构改动（幂等）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印将要执行的 DDL，不真的执行"
    )
    args = parser.parse_args()

    done = skipped = 0
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            for table, column, ddl in COLUMNS:
                if _column_exists(cur, table, column):
                    print(f"⏭️  {table}.{column} 已存在")
                    skipped += 1
                    continue
                print(f"{'（dry-run）' if args.dry_run else ''}➕ {table}.{column}")
                if not args.dry_run:
                    cur.execute(ddl)
                done += 1

            for table, index, ddl in INDEXES:
                if _index_exists(cur, table, index):
                    print(f"⏭️  索引 {table}.{index} 已存在")
                    skipped += 1
                    continue
                print(f"{'（dry-run）' if args.dry_run else ''}➕ 索引 {table}.{index}")
                if not args.dry_run:
                    cur.execute(ddl)
                done += 1

            for table, ddl in TABLES:
                if _table_exists(cur, table):
                    print(f"⏭️  表 {table} 已存在")
                    skipped += 1
                    continue
                print(f"{'（dry-run）' if args.dry_run else ''}➕ 表 {table}")
                if not args.dry_run:
                    cur.execute(ddl)
                done += 1

    tail = "（dry-run：什么都没改）" if args.dry_run else ""
    print(f"\n✅ 完成：变更 {done} 项、跳过 {skipped} 项{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
