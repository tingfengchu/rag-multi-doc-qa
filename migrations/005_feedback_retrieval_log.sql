-- 005_feedback_retrieval_log.sql
-- 模块 6：反馈飞轮 + 运营看板所需的两处存储改动。
--
-- 1) ``feedback.q_hash``：把\"同一个人对同一个问题的第几次评价\"变成可判定的键。
--    没有它，连点两次 👎 会插两行，运营看板上\"差评数\"直接翻倍（实测很容易点出来：
--    用户点了觉得没反应就再点一次）。哈希在**后端**算（services/feedback_service.py
--    的 ``question_hash``），客户端传什么都不影响它。
--
-- 2) ``retrieval_log``：一次问答的检索结果流水。
--    为什么必须有这张表：看板要的四个数字里，有三个（高频提问 / 被检索最多的文档 /
--    检索无结果）在改造前**根本没有落盘**——``chat.py`` 只在 SSE 里发了个 ``no_result``
--    就随连接消失了。没有流水就只能统计\"被评价过的\"那几条（样本极少且偏），
--    或者干脆不做（运营看板上永远空白）。所以这里记\"每一次检索\"：
--      * ``question``          —— 高频提问（同一个问题问了几次）
--      * ``file_uuids``        —— 被命中的文件（JSON 数组，用来算\"被检索最多的文档\"）
--      * ``no_result``         —— 最高分低于拒答阈值 / 一条都没召回
--      * ``max_score``/``hit_count`` —— 排障时\"当时到底检索到几条、多少分\"
--    刻意**不存答案正文**：流水只用来统计与排障，把 LLM 生成的长文本再存一份既费空间
--    又扩大了数据面（要看答案请走 ``feedback`` 表，那是用户明确提交的内容）。
--
-- ⚠️ 幂等性同 004：MySQL 8 没有 ``ADD COLUMN IF NOT EXISTS``，重复执行会报
--    ``1060 Duplicate column name``；要可重复执行请用
--    ``scripts/apply_schema_updates.py``（按 information_schema 只补缺的列/表）。
--
-- ⚠️ 排序规则：与 002/003 保持一致用 ``utf8mb4_unicode_ci``。注意 ``file_dedup``
--    在这台交付机上其实是运行期建的（MySQL 8 默认 ``utf8mb4_0900_ai_ci``），
--    所以**任何跨表 JOIN 都会 1267 报错**（2026-10-13 的事故）——统计代码一律
--    \"分开查 + Python 合并\"，绝不 JOIN（见 services/stats_service.py 的说明）。

USE rag_app;

ALTER TABLE feedback
  ADD COLUMN q_hash CHAR(40) NULL
    COMMENT '问题归一化后的 SHA-1（十六进制 40 位）：同一用户对同一问题只留一条评价',
  ADD KEY idx_user_qhash (user_uuid, q_hash);

-- 检索流水（每次问答一行；命中语义缓存的那次**不写**——见 services/retrieval_log_service.py）
CREATE TABLE IF NOT EXISTS retrieval_log (
  id                 BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_uuid          CHAR(36)     NOT NULL,
  scope              VARCHAR(64)  NOT NULL COMMENT '库 key：public / private:<uuid> / public+private:<uuid>',
  question           TEXT         NOT NULL COMMENT '用户原始问题（统计口径：高频提问）',
  rewritten_question TEXT         NULL COMMENT '改写后的问题（排障用，统计只看 question）',
  hit_count          INT          NOT NULL DEFAULT 0 COMMENT '本次检索召回的块数',
  max_score          FLOAT        NULL COMMENT '最高精排分；NULL = 没有分数（关闭精排/老链路的哨兵）',
  no_result          TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1 = 最高分低于阈值或一条都没召回',
  file_uuids         JSON         NULL COMMENT '本次命中的 file_uuid 列表（统计\"被检索最多的文档\"）',
  created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_created (created_at),
  KEY idx_no_result (no_result, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
