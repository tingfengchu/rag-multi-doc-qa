-- 模块 3（细粒度权限控制）：users.role 增加 authorized_user
--
-- 背景：原 ENUM 只有 ('user','admin')，而模块 3 需要"能写公共库、但不是管理员"的中间角色
-- （可以上传/删除公共库文档，但进不了 /api/admin/*）。
--
-- 为什么不用加一列布尔量（如 can_write_public）：角色是**互斥**状态，
-- 加一列就会出现 "role=admin 且 can_write_public=0" 这类无法解释的组合，
-- 每个判断点都要同时看两处。扩 ENUM 是纯增量变更：旧行不受影响（默认仍是 'user'）。
--
-- 与代码的对应关系：utils/roles.py 的三个常量必须与本 ENUM **逐字一致**
-- （tests/unit/test_user_roles.py 有断言钉死，改一处而不改另一处会立刻变红）。
-- 应用顺序：001 → 002 → 003（重复执行本文件是安全的，MODIFY 幂等）。

USE rag_app;

ALTER TABLE users
  MODIFY COLUMN role ENUM('user','authorized_user','admin') NOT NULL DEFAULT 'user';

-- 自检：应输出 3 个取值 user / authorized_user / admin
SHOW COLUMNS FROM users LIKE 'role';
