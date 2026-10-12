"""密码哈希与强度校验（纯函数，不 import Streamlit / MySQL，可完全离线单测）。

为什么单独抽到 ``utils`` 里，而不是继续写在 ``auth.py``：

1. ``auth.create_user``（注册）和 ``auth.change_password``（改密码）**必须共用同一套规则**。
   规则散在两处必然漂移——"注册有校验、改密码没校验"是最典型的口令漏洞；
2. 本模块只依赖 ``bcrypt``，所以 ``tests/unit/test_passwords.py`` 可以离线跑，
   将来 FastAPI 的 ``api/routers/auth.py`` 也能直接复用，不必反向依赖 ``auth.py``。

⚠️ bcrypt 算法只处理密码的**前 72 字节**：超长密码必须在这里显式拒绝，
而不是依赖截断兜底——否则两个仅在第 73 字节之后不同的密码可以互相登录。
"""
from typing import Tuple

import bcrypt

BCRYPT_MAX_BYTES = 72
MIN_PASSWORD_LENGTH = 8


def _truncate_password(plain: str) -> bytes:
    """bcrypt 最多处理 72 字节，超出部分截断（兜底：正常路径已被 validate 拦住）"""
    return (plain or "").encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    """生成 bcrypt 哈希（每次调用都带新的随机 salt，同一密码两次结果不同）"""
    return bcrypt.hashpw(_truncate_password(plain), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码是否匹配哈希；哈希为空/损坏时返回 False，绝不抛异常"""
    try:
        return bcrypt.checkpw(_truncate_password(plain), (hashed or "").encode())
    except Exception:
        return False


def validate_password_strength(plain: str) -> Tuple[bool, str]:
    """密码强度规则（注册 / 改密码共用）：返回 ``(是否通过, 提示文案)``。

    文案与历史版本 ``create_user`` 里的完全一致，前端可直接展示。
    """
    if not plain:
        return False, "密码不能为空"
    if len(plain) < MIN_PASSWORD_LENGTH:
        return False, f"密码至少 {MIN_PASSWORD_LENGTH} 位"
    if len(plain.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False, f"密码不能超过 {BCRYPT_MAX_BYTES} 字节"
    return True, ""
