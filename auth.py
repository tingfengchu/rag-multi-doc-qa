from typing import Optional, Dict, Tuple
import bcrypt
import streamlit as st
from db import get_conn
from utils.ids import new_uuid
from utils.logger import logger

ALLOWED_ROLES = {"user", "admin"}
BCRYPT_MAX_BYTES = 72


def _truncate_password(plain: str) -> bytes:
    """bcrypt 最多处理 72 字节，超出部分截断"""
    raw = plain.encode("utf-8")
    return raw[:BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_truncate_password(plain), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_truncate_password(plain), hashed.encode())
    except Exception:
        return False


def create_user(username: str, password: str, role: str = "user") -> Tuple[bool, str]:
    # 1. 用户名校验
    username = (username or "").strip()
    if not username:
        return False, "用户名不能为空"
    if len(username) > 64:
        return False, "用户名不能超过 64 个字符"

    # 2. 密码校验
    if not password:
        return False, "密码不能为空"
    if len(password) < 8:
        return False, "密码至少 8 位"
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False, f"密码不能超过 {BCRYPT_MAX_BYTES} 字节"

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