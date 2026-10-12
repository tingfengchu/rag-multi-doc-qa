-- 002_feedback_file_dedup.sql
-- 补全 001_init.sql 中缺失的两张业务表：file_dedup（文件状态机/去重）与 feedback（用户反馈）。
-- 依据代码调用点重建：services/file_state.py、services/feedback_service.py、app.py

CREATE DATABASE IF NOT EXISTS rag_app
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE rag_app;

-- 文件状态机：pending -> success / failed，用于去重与失败补偿
CREATE TABLE IF NOT EXISTS file_dedup (
  id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  file_hash   CHAR(64)     NOT NULL COMMENT '文件内容 SHA-256 十六进制摘要',
  scope       VARCHAR(64)  NOT NULL COMMENT '隔离域：public 或用户 user_uuid',
  file_name   VARCHAR(255) NOT NULL,
  source      VARCHAR(512) NOT NULL COMMENT '{owner_uuid}__{file_uuid}__{filename}',
  owner_uuid  CHAR(36)     NOT NULL,
  file_uuid   CHAR(36)     NOT NULL,
  status      ENUM('pending','success','failed') NOT NULL DEFAULT 'pending',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_file_uuid_scope (file_uuid, scope),
  KEY idx_hash_scope_status (file_hash, scope, status),
  KEY idx_scope_status (scope, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 问答反馈 / bad case 记录
CREATE TABLE IF NOT EXISTS feedback (
  id                 BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_uuid          CHAR(36)     NOT NULL,
  scope              VARCHAR(64)  NOT NULL COMMENT '隔离域：public 或用户 user_uuid',
  question           TEXT         NOT NULL,
  rewritten_question TEXT         NULL,
  answer             MEDIUMTEXT   NULL,
  citations          JSON         NULL COMMENT 'json.dumps(citations) 结果',
  rating             ENUM('up','down') NOT NULL,
  reason             TEXT         NULL,
  file_uuid_filter   CHAR(36)     NULL,
  created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_rating_created (rating, created_at),
  KEY idx_user_scope (user_uuid, scope)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
