"""创建管理员账号（手工初始化用）。

用法（项目根目录下）::

    python scripts/create_admin.py                          # 建默认 admin / ChangeMe_123!
    python scripts/create_admin.py -u e2e_admin_x -p Xxx_123!   # 建指定账号（E2E 用）

为什么加 ``--username`` / ``--password``（2026-10 的文件管理模块）：
    E2E 的"管理员看到全部文件 + 下载"这条用例需要一个**可丢弃的管理员账号**，
    而自助注册只能建普通用户（``routers/auth.py`` 不接受 role），
    于是走这条 CLI：账号名用 ``e2e_`` 前缀 → 会被 ``scripts/purge_e2e_users.py``
    的保洁白名单收走，跑完 E2E 不留垃圾。默认行为（不带参数）与以前逐字一致。
"""
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，输出 emoji 会抛 UnicodeEncodeError，这里强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from auth import create_user

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "ChangeMe_123!"


def main() -> int:
    parser = argparse.ArgumentParser(description="创建管理员账号")
    parser.add_argument("-u", "--username", default=DEFAULT_USERNAME, help="账号名（默认 admin）")
    parser.add_argument("-p", "--password", default=DEFAULT_PASSWORD, help="密码（默认 ChangeMe_123!）")
    args = parser.parse_args()

    ok, msg = create_user(args.username, args.password, role="admin")
    if ok:
        if args.username == DEFAULT_USERNAME:
            print(
                f"✅ 管理员创建成功，默认密码 {DEFAULT_PASSWORD}。\n"
                "⚠️ 请登录后点左侧边栏「🔑 修改密码」立即改成你自己的密码——这是本该有的入口，"
                "以前没有，导致默认密码一直没换。"
            )
        else:
            print(f"✅ 管理员 {args.username} 创建成功")
        return 0
    print(f"❌ 创建失败：{msg}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
