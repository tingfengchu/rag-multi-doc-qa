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

ALLOWED_ROLES = {"user", "admin"}


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
        return False, f"角色必须是 {ALLOWED_ROLES} 之一"

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