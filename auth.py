from typing import Optional, Dict, Tuple  
import bcrypt
import streamlit as st
from db import get_conn
from utils.ids import new_uuid
from utils.logger import logger

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode())
    except Exception:
        return False

def create_user(username: str, password: str, role: str = "user") -> Tuple[bool, str]:
    if not username or not password:
        return False, "用户名和密码不能为空"
    if len(password) < 8:
        return False, "密码至少 8 位"
    user_uuid = new_uuid()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_uuid, username, password_hash, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (user_uuid, username.strip(), hash_password(password), role),
                )
        logger.info("user_created", extra={"username": username, "role": role})
        return True, user_uuid
    except Exception as e:
        logger.warning("user_create_failed", extra={"err": str(e)})
        if "Duplicate entry" in str(e):
            return False, "用户名已存在"
        return False, f"注册失败：{e}"

def authenticate(username: str, password: str) -> Optional[Dict]:
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT user_uuid, username, password_hash, role, is_active "
                "FROM users WHERE username = %s",
                (username.strip(),),
            )
            row = cur.fetchone()
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
                        st.success(f"注册成功：{msg}，请登录")
                    else:
                        st.error(msg)

    st.stop()