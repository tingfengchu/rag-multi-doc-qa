"""清理测试账号（``e2e_*`` / ``test_*``）：E2E 与集成测试自建账号的**保洁**。

用法（项目根目录下）：
    python scripts/purge_e2e_users.py             # 清理并打印条数
    python scripts/purge_e2e_users.py --dry-run   # 只统计，不删除

为什么需要它：E2E 用例通过 ``POST /api/auth/register`` 建临时账号（``e2e_scope_*``、
``e2e_rerank_*`` …），而用户账号**没有删除接口**（也没有"自我注销"的设计），
于是每跑一轮就多攒几个——管理端用户列表越拉越长，这些账号既不是测试数据的产物、
也不是业务数据，就是垃圾。

被谁调用（同一份实现，避免三处各写一遍 SQL）：

* ``tests/conftest.py`` 的 session 夹具（pytest 跑完清一次）；
* 前端 E2E 的 ``e2e/global-teardown.ts``（Playwright 跑完清一次，走本脚本的 CLI）；
* 手工执行（交付前的一次性保洁）。

⚠️ **只删白名单前缀**：绝不按"最近创建""没有文档"这类判据删——那种判据迟早会
删到真人账号，而这是删用户表，不可逆。
"""
import argparse
import os
import sys

#: 项目根目录：``config`` 是按**当前目录**去找 .env / rag_config.txt 的，
#: 所以脚本自己先把 CWD 摆正——手工保洁时不该要求"你必须先 cd 到项目根"（实测踩过）。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

#: 允许清理的用户名前缀白名单（测试自己建的账号都长这样）
TEST_USER_PREFIXES = ("e2e_", "test_")


def _conn():
    """取数据库连接（**延迟 import** + 摆正 CWD，见 ``PROJECT_ROOT`` 的注释）。"""
    os.chdir(PROJECT_ROOT)
    from db import get_conn

    return get_conn()


def count_test_users(prefixes=TEST_USER_PREFIXES) -> int:
    """只统计、不删除的条数（``--dry-run`` 与交付前核对用）。"""
    total = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for prefix in prefixes:
                cur.execute(
                    "SELECT COUNT(*) FROM users WHERE username LIKE %s", (f"{prefix}%",)
                )
                total += int(cur.fetchone()[0])
    return total


def purge_test_users(prefixes=TEST_USER_PREFIXES) -> int:
    """删除测试账号，返回**实际删掉的条数**。

    前缀用 ``%s`` 绑定而不是拼进 SQL：用户名虽然来自测试代码，但拼接等于把
    "注入"这门手艺留给下一个改这里的人；而且 ``LIKE`` 的通配符就是 ``%``，
    拼字符串时还得记得手动转义。
    """
    removed = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for prefix in prefixes:
                cur.execute("DELETE FROM users WHERE username LIKE %s", (f"{prefix}%",))
                removed += cur.rowcount or 0   # rowcount 为 None（某些驱动/语句）时按 0 算
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="清理测试账号（e2e_ / test_ 前缀）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    args = parser.parse_args()

    if args.dry_run:
        print(f"[dry-run] 待清理的测试账号：{count_test_users()} 个")
        return 0
    print(f"已清理测试账号：{purge_test_users()} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
