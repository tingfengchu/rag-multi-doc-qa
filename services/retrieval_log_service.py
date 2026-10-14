"""检索流水（模块 6 的运营看板数据源）：记一次检索，读最近 N 次。

为什么需要这张表：看板要的四个数字里，有三个（高频提问 / 被检索最多的文档 /
检索无结果）在改造前**根本没有落盘**——``api/routers/chat.py`` 只在 SSE 里发了一个
``no_result`` 就随连接消失了。没有流水就只能统计"被评价过的"那几条（样本极少且偏），
或者干脆不做（看板上永远空白）。

三条硬约束：

1. **绝不抛异常**：写流水是旁路，不能因为 MySQL 抖一下就毁掉一次问答
   （``record`` 内部 try/except 全包，失败只留一行 warning）；
2. **命中语义缓存的那次不记**：那次压根没检索。记的话热门问题会把统计刷爆——
   "被检索最多的文档"会变成"缓存命中最多的文档"；
3. **不 JOIN**：与 ``file_dedup`` 的排序规则可能不同（见
   ``services/file_state._usernames_for`` 的 1267 事故），统计一律"分开查 + Python 合并"。
"""
import json
from typing import Dict, Iterable, List, Optional

from db import get_conn
from utils.logger import logger

#: 一条流水最多记多少个 file_uuid：同一个文件命中多个块只算一次，正常情况下远小于它。
#: 定这个上限是为了防止"检索层将来把整个库都召回"时把 JSON 列撑爆。
MAX_STORED_FILE_UUIDS = 50


def record(
    user_uuid: str,
    scope: str,
    question: str,
    rewritten_question: Optional[str],
    hit_file_uuids: Iterable[str],
    hit_count: int,
    max_score: Optional[float],
    no_result: bool,
) -> bool:
    """写一条检索流水。返回是否写成功——**调用方不需要处理失败**（旁路数据）。"""
    uuids: List[str] = []
    for uuid in hit_file_uuids:
        if uuid and uuid not in uuids:
            uuids.append(uuid)
        if len(uuids) >= MAX_STORED_FILE_UUIDS:
            break

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO retrieval_log "
                    "(user_uuid, scope, question, rewritten_question, hit_count, max_score, no_result, file_uuids) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        user_uuid,
                        scope,
                        question,
                        rewritten_question,
                        int(hit_count),
                        None if max_score is None else float(max_score),
                        1 if no_result else 0,
                        json.dumps(uuids, ensure_ascii=False),
                    ),
                )
        return True
    except Exception as e:  # noqa: BLE001 - 旁路数据，绝不影响问答链路
        logger.warning("retrieval_log_failed", extra={"err": str(e), "scope": scope})
        return False


def recent(limit: int) -> List[Dict]:
    """最近 ``limit`` 条流水（新的在前），``file_uuids`` 已解析成 list。

    统计为什么按"最近 N 条"而不是全表：单机部署下流水会一直涨，而看板要的是
    "最近的运营状况"；用 LIMIT 把聚合成本钉死在可控范围内（N 见
    ``services/stats_service.STATS_WINDOW``），比为了让 GROUP BY 走索引而写一堆
    JSON 函数（``JSON_TABLE``）稳得多——那种 SQL 换个 MySQL 小版本就可能报错，
    而它挂掉的表现是"看板整页 500"。
    """
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT question, scope, hit_count, max_score, no_result, file_uuids, created_at "
                "FROM retrieval_log ORDER BY id DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall()

    for row in rows:
        raw = row.get("file_uuids")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                # 脏数据（手工改过 / 写入中断）按"没有命中"处理，不能让它把整页统计带崩
                parsed = []
        else:
            parsed = raw or []
        row["file_uuids"] = [uuid for uuid in parsed if isinstance(uuid, str)]
    return rows
