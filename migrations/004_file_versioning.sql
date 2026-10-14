-- 004_file_versioning.sql
-- 模块 5：文件版本管理。
-- 给 file_dedup 补两个真实列（version / is_archived），把"同名文件的第几个版本"
-- 从**前端按列表顺序数出来的**变成**库里记着的**事实。
--
-- 为什么要动这一步（2026-10-14 交付）：
--   改造前 `GET /files/library` 的 version 是 api/routers/files.py 里 `counter` 累加出来的，
--   它只在"列表恰好按 scope, created_at, id 排序"时才对——一旦想按别的方式排（比如
--   把同名文件聚到一起、或只列最新版），版本号就会跟着乱跳；而且"旧版本被新版本取代"
--   这件事在库里根本不存在，界面无从标记"历史版本"。
--
-- ⚠️ 幂等性：MySQL 8 **没有** `ADD COLUMN IF NOT EXISTS`，重复执行本文件会报
--    `1060 Duplicate column name`。要么忽略该报错（前两列已存在就是"无需再执行"），
--    要么用 `scripts/apply_schema_updates.py`（按 information_schema 判断后只补缺的列，
--    可反复跑，交付/新环境部署都用它）。
--
-- `updated_at` 在 002_feedback_file_dedup.sql 里**已经存在**
--   （`DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP`），
--   所以这里不再重复添加——历史上它确实一直在随 mark_success 的 UPDATE 自动刷新。

USE rag_app;

ALTER TABLE file_dedup
  ADD COLUMN version INT NOT NULL DEFAULT 1
    COMMENT '同名文件在同库中的第几个版本（1 基；同名覆盖上传时 = 旧版本 + 1）',
  ADD COLUMN is_archived TINYINT(1) NOT NULL DEFAULT 0
    COMMENT '1 = 已被同名的新版本取代（历史版本，默认列表不再显示）';

-- 面板默认只列"最新版本"（WHERE ... AND is_archived = 0），这条索引直接服务它；
-- 存量数据（改造前上传的 3 个公共库文件）全部落在默认值上：version=1、is_archived=0，
-- 也就是"每个文件各自 v1、都是最新版"，与改造前的界面表现一致。
ALTER TABLE file_dedup
  ADD KEY idx_scope_name_version (scope, file_name, version);
