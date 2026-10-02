from contextlib import contextmanager
from typing import Optional  # 新增这行
from mysql.connector.pooling import MySQLConnectionPool
from config import settings

_pool: Optional[MySQLConnectionPool] = None  # 把原本的 | None 改成 Optional[...]

def get_pool() -> MySQLConnectionPool:
    global _pool
    if _pool is None:
        _pool = MySQLConnectionPool(
            pool_name="rag_pool",
            pool_size=5,
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_db,
            autocommit=False,
        )
    return _pool

@contextmanager
def get_conn():
    conn = get_pool().get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()