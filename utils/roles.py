"""角色枚举与"能干什么"的**唯一**定义（纯常量，不 import streamlit / MySQL / fastapi）。

为什么要单独一个模块（而不是写在 ``auth.py`` 里）：
    ``auth.py`` 顶部 import 了 streamlit（登录页需要），而 ``api/deps.py`` / ``api/schemas.py``
    只是要拿"哪些角色能写公共库"。角色定义若留在 ``auth.py``，FastAPI 进程就会为了几个字符串
    把整个 streamlit 拖进来——这正是 ``utils/passwords.py`` 当初被拆出去的原因，
    同一套理由在这里第二次成立。

与数据库的关系：``users.role`` 是 ENUM，取值必须与这里**逐字一致**
（``migrations/001_init.sql`` + ``003_user_role_authorized.sql``）。
"代码里有、DB 里没有"的漂移只会在真正授权别人的那一刻炸，所以只留一份定义、两边对齐。
"""
from typing import Final

#: 默认角色：可读公共库、可读写**自己的**私人库，不能写公共库。
ROLE_USER: Final = "user"
#: 被管理员授权的用户：额外获得**写公共库**的能力（上传/删除公共文档），
#: 但没有管理端权限（不能授权他人、不能看服务自检 / RAGAS 面板）。
ROLE_AUTHORIZED_USER: Final = "authorized_user"
#: 管理员：公共库读写 + 全部 ``/api/admin/*`` 端点。
ROLE_ADMIN: Final = "admin"

#: 稳定顺序（报错文案 / 前端下拉都用它）：**不要**用 set 拼文案，集合遍历顺序不保证，
#: 会让"角色必须是 ..."这类提示两次运行都不一样，测试也没法钉死。
ROLE_ORDER: Final = (ROLE_USER, ROLE_AUTHORIZED_USER, ROLE_ADMIN)

#: 角色白名单（校验用）。
ALLOWED_ROLES: Final = frozenset(ROLE_ORDER)

#: 能**写公共库**的角色。写成常量而不是散落在各路由里的 ``role != "admin"``：
#: 写权限会在 upload / delete 等多个端点重复出现，字面量一旦漏改一处，
#: 结果不是越权就是误拒（模块 3 加 ``authorized_user`` 时改的正是这一处）。
WRITE_PUBLIC_ROLES: Final = frozenset({ROLE_ADMIN, ROLE_AUTHORIZED_USER})


def can_write_public(role: str) -> bool:
    """该角色能否写公共库。

    ``api/deps.resolve_scope``（真正拦截）与 ``schemas.UserOut.can_write_public``（告诉前端）
    共用这一个判断：前端拿的是后端算出来的布尔值，不会出现"界面给了选项、接口却 403"的错位。
    """
    return role in WRITE_PUBLIC_ROLES
