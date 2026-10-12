"""重置某个 scope 的知识库数据（ES + Chroma + MySQL file_dedup 三处一起清）。

为什么需要它？
    当前仓库里已经出现过一次"三处数据不一致"：
    Chroma 里有 170 个块，但 file_dedup 是空的（被早期的冒烟测试误删）。
    结果前端"文件管理"空白、重复上传检测失效，用户既看不到也删不掉旧数据。

    另外，给 ES 装上 IK 分词插件之后，旧索引是用 standard 分词器建的，
    必须清空并重新导入文档才能生效。

用法（项目根目录下）：

    # 1) 先看看会删掉什么（不会真的删）
    python scripts/reset_knowledge_base.py --scope public

    # 2) 确认后执行
    python scripts/reset_knowledge_base.py --scope public --yes

    # 3) 如果还换了 ES 分词器/映射，连索引一起删掉重建
    python scripts/reset_knowledge_base.py --scope public --yes --drop-index
"""
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from db import get_conn
from services import es_service, vector_service


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="重置（清空）某个 scope 的知识库数据")
    parser.add_argument("--scope", default="public", help="要重置的 scope，如 public 或 private:<uuid>")
    parser.add_argument("--yes", action="store_true", help="确认执行删除（不加这个参数只是预览）")
    parser.add_argument("--drop-index", action="store_true",
                        help="同时删除整个 ES 索引（下次启动 app 会按当前 IK 可用性重建）")
    return parser.parse_args(argv)


def _count_es(scope: str) -> int:
    try:
        res = es_service.es.count(
            index=es_service.INDEX_NAME,
            query={"bool": {"filter": [{"term": {"metadata.scope": scope}}]}},
        )
        return res["count"]
    except Exception:
        return -1


def _count_mysql(scope: str) -> int:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM file_dedup WHERE scope = %s", (scope,))
                return cur.fetchone()[0]
    except Exception:
        return -1


def main(argv=None) -> int:
    args = _parse_args(argv)
    scope = args.scope

    vectorstore = vector_service.get_vectorstore(scope)
    chroma_count = vector_service.count(vectorstore)
    es_count = _count_es(scope)
    mysql_count = _count_mysql(scope)

    print("=" * 68)
    print(f"将要重置的 scope: {scope}")
    print("-" * 68)
    print(f"  Chroma 向量块      : {chroma_count}")
    print(f"  ES 文档（父+子）    : {es_count}")
    print(f"  MySQL file_dedup   : {mysql_count}")
    print(f"  删除整个 ES 索引    : {'是' if args.drop_index else '否'}")
    print("=" * 68)

    if not args.yes:
        print("这是预览模式，什么都没有删除。确认无误后加上 --yes 再跑一次。")
        return 0

    print("开始清理 ...")

    chroma_deleted = vector_service.delete_all(vectorstore)
    print(f"  ✓ Chroma 删除 {chroma_deleted} 个块")

    if args.drop_index:
        dropped = es_service.drop_index()
        print(f"  ✓ ES 索引删除：{'成功' if dropped else '索引不存在，跳过'}")
    else:
        print(f"  ✓ ES 删除 {es_service.delete_by_scope(scope)} 个文档")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM file_dedup WHERE scope = %s", (scope,))
            deleted_rows = cur.rowcount
    print(f"  ✓ MySQL file_dedup 删除 {deleted_rows} 行")

    print("\n[OK] 重置完成。")
    if args.drop_index:
        print("     下次启动 app 时会按当前 IK 可用性自动重建索引，然后重新上传文档即可。")
    else:
        print("     现在可以重新上传文档了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
