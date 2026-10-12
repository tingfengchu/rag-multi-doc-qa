import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，输出 emoji 会抛 UnicodeEncodeError，这里强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from auth import create_user

if __name__ == "__main__":
    ok, msg = create_user("admin", "ChangeMe_123!", role="admin")
    if ok:
        print(
            "✅ 管理员创建成功，默认密码 ChangeMe_123!。\n"
            "⚠️ 请登录后点左侧边栏「🔑 修改密码」立即改成你自己的密码——这是本该有的入口，"
            "以前没有，导致默认密码一直没换。"
        )
    else:
        print(f"❌ 创建失败：{msg}")