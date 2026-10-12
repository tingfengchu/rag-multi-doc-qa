import json
import redis
from config import settings
import hashlib

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
KB_VERSION = "v4"

def _build_cache_key(scope: str, question: str, filter_key: str = "all"):
    """构造唯一缓存 Key：scope + 版本号 + 文件过滤 + 问题哈希"""
    q_hash = hashlib.md5(question.encode('utf-8')).hexdigest()
    filter_part = filter_key if filter_key else "all"
    return f"cache:{scope}:{KB_VERSION}:{filter_part}:{q_hash}"


def get_cached_answer(scope: str, question: str, filter_key: str = "all"):
    """获取语义缓存（带文件过滤维度）"""
    key = _build_cache_key(scope, question, filter_key)
    raw = r.get(key)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def set_cached_answer(scope: str, question: str, answer: str, citations: list, debug_info: list, filter_key: str = "all"):
    """写入语义缓存（带文件过滤维度）"""
    key = _build_cache_key(scope, question, filter_key)
    payload = {
        "answer": answer,
        "citations": citations,
        "debug_info": debug_info,
        "filter_key": filter_key
    }
    r.setex(key, 86400, json.dumps(payload, ensure_ascii=False))

def clear_all_history():
    """应用启动时清空所有用户的对话历史，但保留语义缓存"""
    # 使用 scan_iter 而不是 KEYS，避免阻塞 Redis
    keys = list(r.scan_iter("chat_history:*"))
    if keys:
        r.delete(*keys)
        print(f"🧹 已清空 {len(keys)} 条对话历史（语义缓存已保留）")
    else:
        print("🧹 暂无对话历史需要清理")