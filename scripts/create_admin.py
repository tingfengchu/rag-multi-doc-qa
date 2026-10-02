import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import create_user

if __name__ == "__main__":
    ok, msg = create_user("admin", "ChangeMe_123!", role="admin")
    if ok:
        print("✅ 管理员创建成功，密码为 ChangeMe_123!，请尽快登录修改。")
    else:
        print(f"❌ 创建失败：{msg}")