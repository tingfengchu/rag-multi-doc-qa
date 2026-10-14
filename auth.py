"""账号与鉴权：注册 / 登录 / 改密码 + Streamlit 登录页。

* 纯逻辑（密码规则、建号、校验、改密码）都在这里和 ``utils.passwords`` 中，
  不含任何 UI 代码——将来迁到 FastAPI 时 ``api/routers/auth.py`` 可直接复用；
* ``require_login()`` 是**唯一**依赖 Streamlit 的函数（登录 / 注册页），
  UI 层换成 React 后，删掉它即可，其余函数一行都不用改。
"""
from typing import Optional, Dict, Tuple

import streamlit as st
from db import get_conn
from utils.ids import new_uuid
from utils.logger import logger

# 密码哈希 / 强度校验统一在 utils.passwords（纯函数、可离线单测、注册与改密码共用一套规则）。
# BCRYPT_MAX_BYTES 一并转出，保持 `from auth import BCRYPT_MAX_BYTES` 这类历史用法不失效。
from utils.passwords import (
    BCRYPT_MAX_BYTES,  # noqa: F401  (re-export)
    hash_password,
    validate_password_strength,
    verify_password,
)

# 角色枚举（模块 3）统一放在 utils.roles：那边不 import streamlit / MySQL，
# 所以 api/deps.py 与 api/schemas.py 可以直接复用，不必为了几个字符串把 streamlit 拖进 API 进程。
# 这里转出（re-export）保持 `from auth import ALLOWED_ROLES` 这类历史用法不失效。
from utils.roles import (  # noqa: F401  (re-export)
    ALLOWED_ROLES,
    ROLE_ADMIN,
    ROLE_AUTHORIZED_USER,
    ROLE_ORDER,
    ROLE_USER,
)



def create_user(username: str, password: str, role: str = "user") -> Tuple[bool, str]:
    # 1. 用户名校验
    username = (username or "").strip()
    if not username:
        return False, "用户名不能为空"
    if len(username) > 64:
        return False, "用户名不能超过 64 个字符"

    # 2. 密码校验（规则统一在 utils.passwords，注册 / 改密码共用同一套，不会各改一半）
    ok, msg = validate_password_strength(password)
    if not ok:
        return False, msg

    # 3. 角色白名单
    role = (role or "user").strip().lower()
    if role not in ALLOWED_ROLES:
        return False, f"角色必须是 {', '.join(ROLE_ORDER)} 之一"

    user_uuid = new_uuid()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_uuid, username, password_hash, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (user_uuid, username, hash_password(password), role),
                )
        logger.info("user_created", extra={"username": username, "role": role})
        return True, user_uuid
    except Exception as e:
        logger.warning("user_create_failed", extra={"err": str(e)})
        if "Duplicate entry" in str(e):
            return False, "用户名已存在"
        return False, "注册失败，请稍后重试"


def set_user_role(
    user_uuid: str, role: str, operator_uuid: Optional[str] = None
) -> Tuple[bool, str]:
    """管理员修改**他人**角色（模块 3）。返回 ``(ok, 中文文案)``，与 ``create_user`` 同一约定。

    三道闸门，每一道都对应一次真实会发生的运维事故：

    1. **角色白名单**：``users.role`` 是 ENUM，写进不认识的值在 MySQL strict mode 下直接报错
       （变成 500 + 一段看不懂的 SQL 报错），必须在业务层先拦；
    2. **不能改自己**：会话里存的是**登录那一刻**的角色，管理员把自己降级后，前端拿的是旧角色、
       后端能力却已改变；更致命的是"最后一名管理员自我降级"——系统从此没人能授权了；
    3. **最后一名在册管理员不许被降级**：第 2 条只挡住"自己"，别人照样能把唯一的管理员拉下来，
       所以必须回库里数一遍（``is_active = 1`` 才算在册）。
    """
    user_uuid = (user_uuid or "").strip()
    if not user_uuid:
        return False, "缺少 user_uuid"
    if not operator_uuid:
        # 路由层一定从会话里取（拿不到就是代码写错了）：这里兜底成"拒绝"而不是"放行"
        return False, "缺少操作者信息"

    role = (role or "").strip().lower()
    if role not in ALLOWED_ROLES:
        return False, f"角色必须是 {', '.join(ROLE_ORDER)} 之一"
    if user_uuid == operator_uuid:
        return False, "不能修改自己的角色，请让另一位管理员操作"

    try:
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(
                    "SELECT username, role FROM users WHERE user_uuid = %s",
                    (user_uuid,),
                )
                row = cur.fetchone()
                if not row:
                    return False, "用户不存在"
                if row["role"] == role:
                    # 幂等：重复点同一个角色不该报错，前端刷新后重放也不会变成"失败"
                    return True, f"{row['username']} 已经是 {role}，无需修改"

                if row["role"] == ROLE_ADMIN:
                    cur.execute(
                        "SELECT COUNT(*) AS admins FROM users "
                        "WHERE role = %s AND is_active = 1",
                        (ROLE_ADMIN,),
                    )
                    count = (cur.fetchone() or {}).get("admins", 0)
                    if count <= 1:
                        return False, "这是最后一名在册管理员，不能改成其它角色"

                cur.execute(
                    "UPDATE users SET role = %s WHERE user_uuid = %s",
                    (role, user_uuid),
                )
        logger.info(
            "user_role_changed",
            extra={"user_uuid": user_uuid, "role": role, "operator": operator_uuid},
        )
        return True, f"已将 {row['username']} 的角色改为 {role}"
    except Exception as e:
        logger.error(
            "user_role_change_failed", extra={"user_uuid": user_uuid, "err": str(e)}
        )
        return False, "修改失败，请稍后重试"


def list_users() -> list:
    """列出全部账号（管理员授权面板用）。

    只取展示必需的四列：**password_hash 绝不出现在任何返回里**——
    这类"顺手 SELECT *"是泄露口令哈希最常见的路径。
    """
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT user_uuid, username, role, is_active, created_at "
                "FROM users ORDER BY created_at ASC, username ASC"
            )
            return list(cur.fetchall())


def get_user(user_uuid: str) -> Optional[Dict]:
    """按 uuid 读单个账号（不含 password_hash）；不存在返回 ``None``。

    改角色后前端要立刻刷新那一行状态，用这个比把整个列表再拉一遍干净。
    """
    user_uuid = (user_uuid or "").strip()
    if not user_uuid:
        return None
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT user_uuid, username, role, is_active, created_at "
                "FROM users WHERE user_uuid = %s",
                (user_uuid,),
            )
            return cur.fetchone()


def authenticate(username: str, password: str) -> Optional[Dict]:
    username = (username or "").strip()
    if not username or not password:
        return None

    try:
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(
                    "SELECT user_uuid, username, password_hash, role, is_active "
                    "FROM users WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
    except Exception as e:
        logger.error("auth_db_error", extra={"err": str(e)})
        return None

    if not row or not row["is_active"]:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return {
        "user_uuid": row["user_uuid"],
        "username": row["username"],
        "role": row["role"],
    }


def change_password(
    user_uuid: str,
    old_password: str,
    new_password: str,
    confirm_password: Optional[str] = None,
) -> Tuple[bool, str]:
    """修改密码：旧密码校验 → 新密码强度校验 → 落库。返回 ``(是否成功, 提示文案)``。

    与 ``create_user`` 保持同一返回约定，文案可直接展示给用户。

    设计取舍：

    * **必须提供旧密码**：如果只凭已登录的 ``user_uuid`` 就能改密码，
      会话被劫持（或别人临时用了一下你的电脑）等于账号直接被夺走；
    * **新密码不能与旧密码相同**：避免"以为改了其实没改"的假安全；
    * 被拒绝的路径一律**不写库**，且与"当前密码不正确"区分文案，
      便于前端给出可操作的提示（但都不暴露账号是否存在之外的额外信息）。
    """
    user_uuid = (user_uuid or "").strip()
    if not user_uuid:
        return False, "会话已失效，请重新登录"

    if not old_password:
        return False, "请输入当前密码"

    ok, msg = validate_password_strength(new_password)
    if not ok:
        return False, msg

    if confirm_password is not None and new_password != confirm_password:
        return False, "两次输入的新密码不一致"

    if new_password == old_password:
        return False, "新密码不能与当前密码相同"

    try:
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(
                    "SELECT password_hash FROM users WHERE user_uuid = %s",
                    (user_uuid,),
                )
                row = cur.fetchone()
                if not row:
                    return False, "账号不存在，请重新登录"
                if not verify_password(old_password, row["password_hash"]):
                    logger.warning(
                        "password_change_rejected",
                        extra={"user_uuid": user_uuid, "reason": "old_password_mismatch"},
                    )
                    return False, "当前密码不正确"

                # updated_at 由表定义的 ON UPDATE CURRENT_TIMESTAMP 自动刷新
                cur.execute(
                    "UPDATE users SET password_hash = %s WHERE user_uuid = %s",
                    (hash_password(new_password), user_uuid),
                )
        logger.info("password_changed", extra={"user_uuid": user_uuid})
        return True, "密码修改成功"
    except Exception as e:
        logger.error(
            "password_change_failed", extra={"user_uuid": user_uuid, "err": str(e)}
        )
        return False, "修改失败，请稍后重试"


def require_login() -> Dict:
    if "user" in st.session_state:
        return st.session_state["user"]

    st.title("🔐 多租户 RAG 系统登录")
    tab_login, tab_register = st.tabs(["登录", "注册"])

    with tab_login:
        with st.form("login_form"):
            u = st.text_input("用户名")
            p = st.text_input("密码", type="password")
            if st.form_submit_button("登录"):
                info = authenticate(u, p)
                if info:
                    st.session_state["user"] = info
                    st.rerun()
                else:
                    st.error("用户名或密码错误")

    with tab_register:
        with st.form("register_form"):
            u2 = st.text_input("新用户名")
            p2 = st.text_input("新密码（≥8 位）", type="password")
            p3 = st.text_input("确认密码", type="password")
            if st.form_submit_button("注册"):
                if p2 != p3:
                    st.error("两次密码不一致")
                else:
                    ok, msg = create_user(u2, p2)
                    if ok:
                        st.success("注册成功，请登录")
                    else:
                        st.error(msg)

    st.stop()