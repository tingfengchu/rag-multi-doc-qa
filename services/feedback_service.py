"""反馈落库 / 查询（模块 6：反馈飞轮 + 运营看板的数据源）。

两条约定，别在别处再实现一遍：

1. **防重键是 `q_hash`**：问题归一化后的 SHA-1（``question_hash``）。前端拿它做
   「已反馈」的幂等键，后端拿它做**短窗口**防重（同一个人对同一个问题在
   ``DEDUPE_WINDOW_MINUTES`` 内只留一条记录）。为什么是窗口而不是永久去重：
   连点两次 👎（用户觉得没反应又点一次）必须只算一次，而"同一个问题两周后又被差评"
   是**真信号**（同一处资料一直没补），不该被合并掉。
2. **不做 JOIN**：本表与 ``file_dedup`` / ``users`` 的排序规则可能不同（见
   ``services/file_state._usernames_for`` 的 1267 事故），统计一律单表聚合。
"""
import hashlib
import json
import re
from typing import Dict, List, Optional

from db import get_conn
from utils.logger import logger

#: 防重窗口（分钟）：只挡"手抖重复提交"，不吞掉跨天的重复差评
DEDUPE_WINDOW_MINUTES = 10

#: 空白折叠：中文问题里的换行/连续空格在复制粘贴与语音输入时非常常见
_WHITESPACE = re.compile(r"\s+")


def normalize_question(question: str) -> str:
    """问题归一化：折叠所有空白、去首尾、英文统一小写。

    不归一化的话，"张嘉亮是谁？"、"张嘉亮是谁？ "、"张嘉亮  是谁？"（中间多一个空格）
    在统计里会被算成三个不同问题，运营看板上的"高频提问"因此永远排不出一条像样的。
    """
    return _WHITESPACE.sub(" ", question or "").strip().casefold()


def question_hash(question: str) -> str:
    """问题归一化后的 SHA-1（40 位十六进制）——👍/👎 的防重键。

    为什么是哈希而不是问题原文：问题存在 ``TEXT`` 列里，直接用原文做等值比较既长又
    受排序规则影响；40 字节定长十六进制串在任何排序规则下都比较得动，也方便放进
    ``feedback.q_hash`` 并建索引。

    ⚠️ 它**不是安全边界**（不加盐、不追求抗碰撞）：作用只有"同一个问题认成同一个"。
    """
    return hashlib.sha1(normalize_question(question).encode("utf-8")).hexdigest()


def find_by_hash(
    user_uuid: str, q_hash: str, window_minutes: int = DEDUPE_WINDOW_MINUTES
) -> Optional[Dict]:
    """找该用户**在防重窗口内**对同一个问题的最近一条反馈（没有则 None）。

    ``POST /feedback`` 用它算响应的 ``updated`` 字段（是新建还是"改上一次的评价"）。
    """
    if not q_hash:
        return None
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT id, rating, created_at FROM feedback "
                "WHERE user_uuid = %s AND q_hash = %s "
                "AND created_at > (NOW() - INTERVAL %s MINUTE) "
                "ORDER BY id DESC LIMIT 1",
                (user_uuid, q_hash, int(window_minutes)),
            )
            return cur.fetchone()


def save_feedback(user_uuid: str, scope: str, question: str,
                  rewritten_question: str, answer: str,
                  citations: list, rating: str, reason: str = None,
                  file_uuid_filter: str = None):
    """写一条反馈（返回值保持 ``True`` / ``False``：Streamlit 版 ``app.py`` 靠真假值判断成败）。

    防重（模块 6）：窗口内的重复提交走 **UPDATE**（改评价 / 补充原因），窗口外照常
    INSERT。前者挡掉"连点两次 👎 变成两条差评"（运营看板上直接翻倍），后者保住
    "同一个问题不同时间的评价历史"。

    ``created_at`` 用 DB 默认值，UPDATE 时**不刷新它**——它表示这条记录第一次出现的
    时间；被 UPDATE 冲掉的话，看板上"最近差评"的排序会随一次改评价而跳来跳去。
    """
    q_hash = question_hash(question)
    try:
        existing = find_by_hash(user_uuid, q_hash)
        with get_conn() as conn:
            with conn.cursor() as cur:
                if existing:
                    cur.execute(
                        "UPDATE feedback SET scope = %s, rewritten_question = %s, answer = %s, "
                        "citations = %s, rating = %s, reason = %s, file_uuid_filter = %s "
                        "WHERE id = %s",
                        (scope, rewritten_question, answer,
                         json.dumps(citations, ensure_ascii=False), rating, reason,
                         file_uuid_filter, existing["id"]),
                    )
                    action = "updated"
                else:
                    cur.execute(
                        "INSERT INTO feedback "
                        "(user_uuid, scope, question, rewritten_question, answer, citations, rating, reason, file_uuid_filter, q_hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (user_uuid, scope, question, rewritten_question, answer,
                         json.dumps(citations, ensure_ascii=False), rating, reason,
                         file_uuid_filter, q_hash),
                    )
                    action = "created"
        logger.info("feedback_saved", extra={"rating": rating, "scope": scope, "action": action})
        return True
    except Exception as e:
        logger.error("feedback_failed", extra={"err": str(e)})
        return False


def get_bad_cases(limit: int = 100):
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT * FROM feedback WHERE rating = 'down' "
                "ORDER BY created_at DESC LIMIT %s", (limit,)
            )
            return cur.fetchall()


def summary() -> Dict:
    """好评 / 差评统计（运营看板，模块 6）。

    三条**单表**聚合查询 + Python 拼装（在自己表里 JOIN 自己也没必要）：
    按评价类型总数、按库 × 评价类型、差评原因 Top 5。

    ``up_rate`` 在没有反馈时返回 ``None`` 而不是 0：0% 与"还没有人评价"在界面上必须
    长得不一样，否则新部署的看板一进来就是一片"好评率 0%"，看着像答得很差。
    ``scope`` 只回原串（``public`` / ``both`` / ``private:<uuid>``）——中文库名交给
    前端 ``scopeLabel``（与后端 ``deps.describe_scope`` 同一份口径）。
    """
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT rating, COUNT(*) AS n FROM feedback GROUP BY rating")
            by_rating = {row["rating"]: int(row["n"]) for row in cur.fetchall()}

            cur.execute(
                "SELECT scope, rating, COUNT(*) AS n FROM feedback GROUP BY scope, rating"
            )
            scope_rows = cur.fetchall()

            cur.execute(
                "SELECT reason, COUNT(*) AS n FROM feedback "
                "WHERE rating = 'down' AND reason IS NOT NULL AND reason <> '' "
                "GROUP BY reason ORDER BY n DESC, reason LIMIT 5"
            )
            reason_rows = cur.fetchall()

    up, down = by_rating.get("up", 0), by_rating.get("down", 0)
    total = up + down

    scopes: Dict[str, Dict[str, int]] = {}
    for row in scope_rows:
        bucket = scopes.setdefault(row["scope"], {"up": 0, "down": 0})
        bucket[row["rating"]] = bucket.get(row["rating"], 0) + int(row["n"])

    by_scope: List[Dict] = [
        {"scope": scope, "up": counts["up"], "down": counts["down"]}
        for scope, counts in scopes.items()
    ]
    # 排序固定（先按样本量、再按库名）：同一份数据两次刷新顺序必须一样，
    # 否则看板每次刷新都在"跳动"，而且单测也没法钉死。
    by_scope.sort(key=lambda item: (-(item["up"] + item["down"]), item["scope"]))

    return {
        "total": total,
        "up": up,
        "down": down,
        # 保留 4 位小数：0.6667 比"67%"更能说明"样本很少"这件事
        "up_rate": round(up / total, 4) if total else None,
        "by_scope": by_scope,
        "top_reasons": [{"reason": row["reason"], "count": int(row["n"])} for row in reason_rows],
    }