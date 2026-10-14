import json
import redis
from config import settings
import hashlib
from utils.logger import logger

# 初始化 Redis 连接
r = redis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    decode_responses=True
)

# 会话过期时间：1小时（对话历史，不持久存）
SESSION_TTL = 3600
# 最多保留最近 10 轮对话，防止上下文爆炸
MAX_HISTORY = 10
# 语义缓存过期时间：24 小时（答案与分数一起到期，不会出现"分数先丢"的半截缓存）
CACHE_TTL = 86400


def get_history(user_uuid: str, scope: str) -> list:
    """获取指定用户+库的对话历史"""
    key = f"chat_history:{user_uuid}:{scope}"
    raw = r.lrange(key, 0, -1)
    return [json.loads(item) for item in raw]


def append_history(user_uuid: str, scope: str, user_msg: str, ai_msg: str):
    """追加一轮对话历史"""
    key = f"chat_history:{user_uuid}:{scope}"
    r.rpush(key, json.dumps({"role": "user", "content": user_msg}, ensure_ascii=False))
    r.rpush(key, json.dumps({"role": "assistant", "content": ai_msg}, ensure_ascii=False))
    # 只保留最近的 MAX_HISTORY*2 条
    r.ltrim(key, -MAX_HISTORY * 2, -1)
    r.expire(key, SESSION_TTL)


def clear_history(user_uuid: str, scope: str):
    """清空对话历史（可选功能）"""
    key = f"chat_history:{user_uuid}:{scope}"
    r.delete(key)

# 知识库版本号（每次知识库大更新，这个数字加1，旧缓存立刻全部失效）
#
# v5 → v6（2026-10-14）：**删除文件后旧缓存还在引用它**。缓存 key 只由 scope + 版本号 +
# 问题哈希决定，改了库内容它一无所知 —— 于是"删除文件 → 用户再问同样的问题 → 命中缓存
# → 回答里仍是那个已删除的文件 → 点引用下载 404"（用户报的事故）。本次加两条防线：
#   ① 精确失效：payload 里记下 `source_file_uuids`，删除接口调
#      `invalidate_cache_by_file_uuid` 把引用该文件的缓存逐条删掉（首选，见 files.py）；
#   ② 抬版本号：精确失效只对**带 uuid 的缓存**有效 —— 本次改造前写入的、以及 Streamlit
#      入口（app.py 的引用只有 source/page）写入的缓存都没有 uuid，只能靠这里一次性作废。
#
# v4 → v5（2026-10-13）：**改了精排归一化口径**（utils/rerank.normalize_scores），
# 而缓存里存着 `rerank_score` / `max_score`（前端显示的置信度）。不抬版本的话，
# 24 小时内命中旧缓存会回放**旧刻度**的分数（MiniLM 那边是 0.9998 这种假满分），
# 用户会以为这次修复没生效 —— 这正是"缓存里存分数"必须和打分口径绑定版本号的原因。
KB_VERSION = "v6"

#: 语义缓存里保存"这次回答的检索最高分"的字段名（值 = ``api`` 层 ``done`` 载荷的 ``max_score``）。
#:
#: 为什么必须存：命中缓存时前端要显示的就是**首次回答**那个置信度。改造前缓存不含分数，
#: 调用方只能写死 1.0，于是同一条答案"首次回答 0.62 / 命中缓存 1.00"——缓存看起来比真实回答
#: 更可信，是最误导人的失真（模块 2 修的就是它）。两端共用这一个常量，改名时不会静默失联。
CACHE_SCORE_KEY = "rerank_score"

#: 语义缓存里保存"这次回答引用了哪些文件"的字段名（值 = 去重后的 `file_uuid` 列表）。
#:
#: 为什么必须存（2026-10-14 用户报的事故）：删除文件后那条答案还在 Redis 里，用户再问
#: 同样的问题就命中缓存 → 回答照旧引用已删除的文件 → 点引用下载 404。缓存 key 只认
#: scope + 版本号 + 问题哈希，**天生看不见"库里的文件被删了"**；要删得精确，就必须把
#: "这条答案依赖哪些文件"和答案一起存下来（`invalidate_cache_by_file_uuid` 据此匹配）。
SOURCE_FILE_UUIDS_KEY = "source_file_uuids"

#: 精确失效时一次 `DEL` 的最大 key 数：分批只是兜底，不让一个巨大的命令卡住 Redis
#: （删除文件是低频动作，缓存总量通常几十~几百条）。
_DELETE_BATCH = 200


def _source_file_uuids(citations) -> list:
    """从 citations 里抽出所有 `file_uuid`：**去重、保序、丢掉空值**。

    容错原则是"宁可少记，不可错记"：

    * 引用里没有 `file_uuid`（本次改造前的缓存、以及 Streamlit 入口写的引用只有
      `source`/`page`）→ 这条引用不贡献任何 uuid，整条缓存退回"只能靠抬版本号失效"；
    * `None` / 空串一律跳过 —— 真把 `None` 记下来，将来一次"空 uuid 的删除请求"
      就可能误伤一片缓存；
    * 非 dict 的脏引用直接跳过，不让写缓存这条路因为一条坏引用而失败。
    """
    uuids: list = []
    seen = set()
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        file_uuid = citation.get("file_uuid")
        if not file_uuid:
            continue
        key = str(file_uuid)
        if key in seen:
            continue
        seen.add(key)
        uuids.append(key)
    return uuids


def _normalize_score(value):
    """把分数规整成 ``float``（保留 4 位小数，与 ``api/routers/chat.py:_done_payload`` 一致）。

    无法解析（``None`` / ``"abc"`` / 本次改造前写入的老缓存缺字段）一律返回 ``None``，
    语义是"分数未知"：调用方据此**不显示**置信度，而不是拿 1.0 冒充满分。
    """
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _build_cache_key(scope: str, question: str, filter_key: str = "all"):
    """构造唯一缓存 Key：scope + 版本号 + 文件过滤 + 问题哈希"""
    q_hash = hashlib.md5(question.encode('utf-8')).hexdigest()
    filter_part = filter_key if filter_key else "all"
    return f"cache:{scope}:{KB_VERSION}:{filter_part}:{q_hash}"


def get_cached_answer(scope: str, question: str, filter_key: str = "all"):
    """获取语义缓存（带文件过滤维度）。

    返回的 dict **一定**包含 ``CACHE_SCORE_KEY`` 键（老缓存 → ``None``），
    这样调用方不必写 ``if "rerank_score" in cache`` 这种漏一次就退回硬编码的判断。
    缓存在读取侧坏掉（非法 JSON / 不是 dict）只当未命中，绝不抛异常——
    缓存是加速手段，不该拖垮一次问答。
    """
    key = _build_cache_key(scope, question, filter_key)
    raw = r.get(key)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload[CACHE_SCORE_KEY] = _normalize_score(payload.get(CACHE_SCORE_KEY))
    return payload


def set_cached_answer(scope: str, question: str, answer: str, citations: list,
                      debug_info: list, filter_key: str = "all", rerank_score=None):
    """写入语义缓存（带文件过滤维度）。

    ``rerank_score`` = 本次回答的检索最高分（``max_score``）。它必须和答案一起落盘，
    否则缓存命中时无法复现首次回答的置信度（正是模块 2 的失真来源）。

    ``citations`` 里的 ``file_uuid`` 会被抽成 ``SOURCE_FILE_UUIDS_KEY`` 一起落盘：
    那是"删文件 → 精确失效"的唯一依据（没有它就只能靠 ``KB_VERSION`` 全量作废）。
    """
    key = _build_cache_key(scope, question, filter_key)
    payload = {
        "answer": answer,
        "citations": citations,
        "debug_info": debug_info,
        "filter_key": filter_key,
        CACHE_SCORE_KEY: _normalize_score(rerank_score),
        # 记下"这条答案引用了哪些文件"：删除文件时才能**精确**失效（见
        # invalidate_cache_by_file_uuid）。空列表是合法值（引用里没 uuid 的老调用方），
        # 它表示"这条缓存无法按文件失效"，而不是"引用了所有文件"。
        SOURCE_FILE_UUIDS_KEY: _source_file_uuids(citations),
    }
    # 用 set(..., ex=) 而不是 setex：redis-py 已把 setex 标记为弃用
    # （DeprecationWarning: Use 'set' instead），且 api/sessions.py 早已是这种写法——
    # 同一个仓库里两种写法只会让下一个写缓存的人继续抄到弃用那个。
    r.set(key, json.dumps(payload, ensure_ascii=False), ex=CACHE_TTL)


def invalidate_cache_by_file_uuid(file_uuid) -> int:
    """删除文件时调用：把所有**引用了该文件**的语义缓存删掉，并返回删除条数。

    为什么必须做（用户报的事故）：删文件只动了 ES / Chroma / MySQL 记录，缓存里那条答案
    还在，用户再问同样的问题就命中缓存 → 回答照旧引用已删除的文件 → 点引用下载 404。

    为什么是"扫一遍"而不是维护反向索引：缓存总量本就很小（每个"问题 + 过滤维度"一条、
    TTL 24 小时），而删除文件是低频动作；反向索引还要额外处理 TTL 过期、key 被覆盖等
    一致性问题，收益为负。**这里真正要保证的是宁可少删、不可错删**：
    payload 读不出来（非法 JSON / 不是 dict / 没有该字段 / 该字段不是列表）一律**保留** ——
    少删一条的代价是"用户多等一次回答"，错删的代价是"缓存整体失效、白烧一次大模型调用"。

    只扫 ``cache:*``：对话历史（``chat_history:*``）是另一回事（由用户主动清），不在这里动。

    调用方（``api/routers/files.py:delete_file``）会在**文件真的消失之前**调用它：
    Redis 不可用时这里抛异常 → 删除整体失败、文件仍在（用户可重试），
    而不是留下"文件已删、缓存还在"这种最难收场的状态（那正是本次要修的 bug）。
    """
    wanted = str(file_uuid or "").strip()
    if not wanted:
        # 空 uuid 只可能是调用方的 bug（而且它会让"是否包含"退化成对空值的判断）
        logger.warning("cache_invalidation_skipped", extra={"reason": "empty_file_uuid"})
        return 0

    scanned = list(r.scan_iter("cache:*"))
    doomed = []
    for key in scanned:
        raw = r.get(key)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            # 坏缓存不删：它可能引用了这个文件，也可能没有 —— 读不出来就不做判断
            continue
        if not isinstance(payload, dict):
            continue
        recorded = payload.get(SOURCE_FILE_UUIDS_KEY)
        # 必须是**列表**才按成员判断：脏数据里该字段可能是字符串，
        # 而 `"A" in "AB"` 会命中子串 —— 那种误删是静默的，最难查
        if isinstance(recorded, list) and wanted in recorded:
            doomed.append(key)

    for start in range(0, len(doomed), _DELETE_BATCH):
        r.delete(*doomed[start:start + _DELETE_BATCH])
    logger.info(
        "cache_invalidated",
        extra={"file_uuid": wanted, "scanned": len(scanned), "deleted": len(doomed)},
    )
    return len(doomed)


def clear_all_history():
    """应用启动时清空所有用户的对话历史，但保留语义缓存"""
    # 使用 scan_iter 而不是 KEYS，避免阻塞 Redis
    keys = list(r.scan_iter("chat_history:*"))
    if keys:
        r.delete(*keys)
    # 原先这里 print 了 🧹 emoji：GBK 控制台 / stdout 被重定向到文件时
    # print(emoji) 会抛 UnicodeEncodeError，而这是**启动路径**——等于服务起不来。
    logger.info("history_cleared", extra={"cleared": len(keys), "cache": "kept"})