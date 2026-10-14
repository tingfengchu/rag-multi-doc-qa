"""运营看板统计（模块 6）：把「检索流水 + 反馈」聚合成 ``GET /api/admin/stats`` 的四个块。

四个块与它们的数据来源：

| 块 | 数据来源 | 说明 |
| --- | --- | --- |
| ``top_questions``    | ``retrieval_log`` | 高频提问（按归一化后的问题聚合） |
| ``top_documents``    | ``retrieval_log.file_uuids`` | 被检索最多的文档（文件名单独查，见下） |
| ``no_result_queries``| ``retrieval_log.no_result`` | 检索无结果的问题（该补资料的信号） |
| ``feedback_summary`` | ``feedback`` | 好评 / 差评统计（``feedback_service.summary``） |

三个刻意的设计取舍：

1. **聚合在 Python 里做，不用 ``JSON_TABLE`` / 跨表 JOIN**。原因很实在：``file_dedup``
   在交付机上是运行期建的（``utf8mb4_0900_ai_ci``），与 migration 建的表
   （``utf8mb4_unicode_ci``）一比就报 ``1267 Illegal mix of collations``
   （2026-10-13 已经在文件面板上翻过一次车）；而 ``JSON_TABLE`` 换个 MySQL 小版本
   就可能报错，挂掉的表现是"看板整页 500"。这里只在**有限窗口**（``limit``）内做
   Python 聚合，成本可控、行为可测。
2. **窗口而非全量**：流水会一直涨，看板要的是"最近的运营状况"。``window`` 参数
   由调用方决定（``GET /admin/stats?window=``），默认 ``STATS_WINDOW``。
3. **被检索到就算命中**：``top_documents`` 统计的是"召回到"的次数，与
   ``no_result`` 不冲突——一次"没找到足够相关信息"的问答里，仍然可能有几个块被
   召回（只是分数不够）。把它排除掉会让"哪些文档总被召回但总不够分"这类最该修
   的信号恰好消失。
"""
from collections import Counter
from datetime import datetime
from typing import Dict, List

from services import feedback_service, file_state, retrieval_log_service

#: 默认统计窗口：最近多少次检索。定这个值而不是全表 GROUP BY 的理由见模块 docstring。
STATS_WINDOW = 5000

#: 每个榜单最多返回多少条（看板不做无限长的表）
TOP_N = 10


def _bucket(rows: List[Dict], predicate=None) -> List[Dict]:
    """按归一化问题聚合成 ``[{question, count, last_at}]``（次数降序，问题升序）。

    ``rows`` 是 ``retrieval_log_service.recent`` 的返回，**新的在前**，
    所以第一次见到某个 key 时的 ``created_at`` 就是它最近一次被问的时间。
    """
    counter: Counter = Counter()
    display: Dict[str, str] = {}
    last_at: Dict[str, object] = {}
    for row in rows:
        if predicate and not predicate(row):
            continue
        key = feedback_service.normalize_question(row["question"])
        if not key:
            continue
        counter[key] += 1
        display.setdefault(key, row["question"])
        last_at.setdefault(key, row["created_at"])

    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [
        {"question": display[key], "count": count, "last_at": last_at.get(key)}
        for key, count in ordered[:TOP_N]
    ]


def top_documents(rows: List[Dict]) -> List[Dict]:
    """被检索最多的文档：``[{file_uuid, file_name, hits}]``。

    文件名走 ``file_state.names_for_uuids`` **单独查一次再合并**（不 JOIN，
    原因见模块 docstring）；文件已被删除时文件名回空串，由前端显示占位文案——
    它确实被检索过，把它从统计里抹掉等于篡改数据。
    """
    counter: Counter = Counter()
    for row in rows:
        # 一次检索里同一个文件被多个块命中只算**一次**（"被检索次数"说的是"有多少次
        # 检索命中了它"，不是"命中了多少个块"）。写入路径已经去过重，这里再兜一层：
        # 流水是脏数据最容易出现的地方（手工改过、写入中断），而统计错了没人看得出来。
        for uuid in dict.fromkeys(row["file_uuids"]):
            counter[uuid] += 1
    if not counter:
        return []

    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:TOP_N]
    names = file_state.names_for_uuids([uuid for uuid, _ in ordered])
    return [
        {"file_uuid": uuid, "file_name": names.get(uuid, ""), "hits": hits}
        for uuid, hits in ordered
    ]


def build_stats(window: int = STATS_WINDOW) -> Dict:
    """看板数据（一次查询窗口 + 一次反馈聚合）。"""
    rows = retrieval_log_service.recent(window)
    return {
        # 界面顶部的"数据截止时间"：运营看到的是快照，不是一个永远实时的数字
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": int(window),
        "retrieval_total": len(rows),
        "top_questions": _bucket(rows),
        "no_result_queries": _bucket(rows, predicate=lambda row: bool(row["no_result"])),
        "top_documents": top_documents(rows),
        "feedback_summary": feedback_service.summary(),
    }
