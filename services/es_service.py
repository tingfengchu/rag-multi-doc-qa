"""Elasticsearch 访问层：索引管理 + BM25 检索 + 父块回溯。

⚠️ 关于中文分词（重要）
    mapping 里用到的 ``ik_max_word`` / ``ik_smart`` 依赖 Elasticsearch 的
    analysis-ik 插件（镜像定义见 ``es/Dockerfile``）。插件缺失时后果是：

    1. 建索引直接 400 报错：``failed to find analyzer [ik_max_word]``；
    2. 就算绕开建索引，``standard`` 分词器会把中文切成单字，
       "张嘉亮的专业技能" -> 张 / 嘉 / 亮 / 的 / 专 / 业 / 技 / 能，
       BM25 的精度会明显下降（专有名词几乎失效）。

    因此 ``init_es_index()`` 会先探测 IK 是否可用：可用就上 IK mapping，
    不可用就自动降级为标准分词器并打 WARNING，保证服务永远能起来。
"""
import logging

from elasticsearch import Elasticsearch

from config import settings

INDEX_NAME = "rag_knowledge"

logger = logging.getLogger("rag.es")

# 初始化ES连接
es = Elasticsearch(
    [f"http://{settings.es_host}:{settings.es_port}"],
    request_timeout=30,
)

# 运行时探测缓存：None 表示还没探测过
_ik_available = None
_index_uses_ik_cache = None


def ik_available() -> bool:
    """探测集群是否安装了 analysis-ik 插件（结果缓存，只探测一次）。"""
    global _ik_available
    if _ik_available is None:
        try:
            es.indices.analyze(analyzer="ik_max_word", text="测试分词")
            _ik_available = True
            logger.info("es_ik_ready", extra={"analyzer": "ik_max_word"})
        except Exception as e:
            _ik_available = False
            logger.warning(
                "es_ik_missing",
                extra={
                    "err": str(e),
                    "hint": (
                        "Elasticsearch 未安装 analysis-ik 插件，中文检索已降级为标准分词器；"
                        "请用 es/Dockerfile 构建带 IK 的 ES 镜像，并重新导入文档。"
                    ),
                },
            )
    return _ik_available


def _build_index_body(use_ik: bool) -> dict:
    """构造索引 settings + mappings。use_ik=False 时完全不引用 IK 分词器。"""
    if use_ik:
        ik_text = {
            "type": "text",
            "analyzer": "ik_max_word_analyzer",
            "search_analyzer": "ik_smart_analyzer",
        }
        content_field = dict(ik_text)
        header_field = dict(ik_text)
        settings_block = {
            "analysis": {
                "analyzer": {
                    "ik_max_word_analyzer": {"type": "custom", "tokenizer": "ik_max_word"},
                    "ik_smart_analyzer": {"type": "custom", "tokenizer": "ik_smart"},
                }
            }
        }
    else:
        content_field = {"type": "text"}
        header_field = {"type": "text"}
        settings_block = {}

    return {
        "settings": settings_block,
        "mappings": {
            "properties": {
                "content": content_field,
                "parent_id": {"type": "keyword"},
                "child_id": {"type": "keyword"},
                "doc_type": {"type": "keyword"},
                "metadata": {
                    "properties": {
                        "scope": {"type": "keyword"},
                        "page": {"type": "integer"},
                        "header": header_field,
                        "file_uuid": {"type": "keyword"},
                        "source": {"type": "keyword"},
                    }
                },
            }
        },
    }


def init_es_index() -> bool:
    """确保索引存在。返回 True 表示本次真的新建了索引。"""
    if es.indices.exists(index=INDEX_NAME):
        return False

    use_ik = ik_available()
    body = _build_index_body(use_ik)
    try:
        es.indices.create(
            index=INDEX_NAME,
            settings=body["settings"] or None,
            mappings=body["mappings"],
        )
    except Exception:
        # 多副本/多进程同时启动时会撞车，只要索引最终存在就算成功
        if es.indices.exists(index=INDEX_NAME):
            return False
        raise

    logger.info(
        "es_index_created",
        extra={"index": INDEX_NAME, "analyzer": "ik_max_word" if use_ik else "standard"},
    )
    # 这里以前还有一行 print("ES 索引 ... 创建成功")：与上面那条 logger.info 完全重复，
    # 而 print 会把同样的信息越过日志通道直接写 stdout（脚本/worker 下编码不可控）。
    return True


def index_uses_ik() -> bool:
    """检查**已存在**的索引是否真的用了 IK 分词器（结果缓存）。

    用于发现"索引是早期没有 IK 时建的、后来装了 IK 但没重建索引"
    这种静默降级状态（警告会显示在 Streamlit 侧边栏）。
    """
    global _index_uses_ik_cache
    if _index_uses_ik_cache is None:
        try:
            mapping = es.indices.get_mapping(index=INDEX_NAME)
            _index_uses_ik_cache = "ik_max_word_analyzer" in str(mapping)
        except Exception:
            _index_uses_ik_cache = False
    return _index_uses_ik_cache


def refresh_index():
    """强制刷新，让刚写入的文档立即可被检索到。

    上传完一批文档后调用一次即可。ES 默认 refresh_interval=1s，
    批量写入后如果立刻提问，有可能搜不到刚入库的内容。
    """
    try:
        es.indices.refresh(index=INDEX_NAME)
    except Exception as e:
        logger.warning("es_refresh_failed", extra={"err": str(e)})


def index_parent_chunk(parent_id: str, content: str, metadata: dict):
    """存入父块（用于回溯）"""
    doc = {
        "parent_id": parent_id,
        "content": content,
        "metadata": metadata,
        "doc_type": "parent"
    }
    es.index(index=INDEX_NAME, id=parent_id, document=doc)


def index_child_chunk(child_id: str, parent_id: str, content: str, metadata: dict):
    """存入子块（用于BM25检索）"""
    doc = {
        "child_id": child_id,
        "parent_id": parent_id,
        "content": content,
        "metadata": metadata,
        "doc_type": "child"
    }
    es.index(index=INDEX_NAME, id=child_id, document=doc)


def search_bm25(query: str, scope: str, top_k: int = 10, file_uuid_filter: str = None):
    """BM25 关键词检索（只搜子块）。

    ``scope`` 是必填的：它承担多租户隔离，保证私人库内容绝不会
    被其它用户的检索命中。
    使用显式关键字参数（query=/size=）而不是已废弃的 body=，
    兼容 elasticsearch-py 8.x/9.x。
    """
    filters = [{"term": {"metadata.scope": scope}}]
    if file_uuid_filter:
        filters.append({"term": {"metadata.file_uuid": file_uuid_filter}})

    res = es.search(
        index=INDEX_NAME,
        query={
            "bool": {
                "must": [
                    {"match": {"content": query}},
                    {"term": {"doc_type": "child"}},
                ],
                "filter": filters,
            }
        },
        size=top_k,
    )
    return res["hits"]["hits"]

def get_parent_chunk(parent_id: str):
    """根据子块的 parent_id 精准拉取完整的父块（找不到返回 None）。"""
    try:
        res = es.get(index=INDEX_NAME, id=parent_id)
        return res["_source"]
    except Exception as e:
        logger.warning("es_get_parent_failed", extra={"parent_id": parent_id, "err": str(e)})
        return None


def _delete_by_query_stats(res) -> dict:
    """把 ``delete_by_query`` 的响应规整成 ``{total, deleted, conflicts, failures}``。

    为什么要规整而不是一句 ``res.get("deleted", 0)``：
    * ``delete_by_query`` 的响应里 ``total``（命中的文档数）才是"应该删掉多少"，
      ``deleted`` 才是"真的删掉了多少"。两者不等就说明有文档没删掉
      （版本冲突 / 局部失败），此时**必须告警**——否则调用方只看到
      "删除成功，共 N 条"，而 ES 里还留着搜得到的残块（表现为"删了还能搜到"），
      排查时没人会想到是版本冲突。
    * 响应对象在不同客户端版本里既可能是 ``ObjectApiResponse``（支持 ``.get``），
      也可能是裸 dict；统一走 ``.body`` 回退，避免因取字段方式不同而静默返回 0
      （返回 0 的后果更糟：文件状态表会被标记成"已删除"，而向量还在）。
    """
    body = getattr(res, "body", None)
    if not isinstance(body, dict):
        body = res if isinstance(res, dict) else {}

    def _as_int(value) -> int:
        # ES 的计数永远是整数；这里再兜一层，防止 None / 字符串把它带进日志与返回值
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "total": _as_int(body.get("total")),
        "deleted": _as_int(body.get("deleted")),
        "conflicts": _as_int(body.get("version_conflicts")),
        "failures": list(body.get("failures") or []),
    }


def _log_delete_result(op: str, stats: dict, **extra) -> int:
    """按"是否删干净"分级记录，并返回真实删除条数。"""
    payload = {**extra, **stats}
    if stats["failures"]:
        # failures 非空 = 有分片直接报错，这部分文档肯定没删掉
        logger.error(f"{op}_failed", extra=payload)
    elif stats["deleted"] != stats["total"] or stats["conflicts"]:
        logger.warning(f"{op}_incomplete", extra=payload)
    else:
        logger.info(op, extra=payload)
    return stats["deleted"]


def delete_by_file_uuid(file_uuid: str, scope: str) -> int:
    """删除某个文件的所有块（父块 + 子块），返回**真实删除**条数。

    refresh=True 让删除立刻可见，否则刚删完还能搜到（表现为"删不掉"）。
    conflicts="proceed" 避免并发写入触发的 VersionConflict 直接中断删除
    （代价是可能留下没删掉的块，所以下面的计数必须如实上报，见 ``_log_delete_result``）。
    """
    res = es.delete_by_query(
        index=INDEX_NAME,
        query={
            "bool": {
                "filter": [
                    {"term": {"metadata.file_uuid": file_uuid}},
                    {"term": {"metadata.scope": scope}},
                ]
            }
        },
        refresh=True,
        conflicts="proceed",
    )
    return _log_delete_result(
        "es_deleted_by_file",
        _delete_by_query_stats(res),
        file_uuid=file_uuid,
        scope=scope,
    )


def delete_by_scope(scope: str) -> int:
    """删除某个 scope 下的全部块，返回**真实删除**条数（用于重置知识库）。"""
    res = es.delete_by_query(
        index=INDEX_NAME,
        query={"bool": {"filter": [{"term": {"metadata.scope": scope}}]}},
        refresh=True,
        conflicts="proceed",
    )
    return _log_delete_result(
        "es_deleted_by_scope", _delete_by_query_stats(res), scope=scope
    )


def drop_index() -> bool:
    """删除整个索引（下次启动 app 会按当前 IK 可用性自动重建）。"""
    try:
        if es.indices.exists(index=INDEX_NAME):
            es.indices.delete(index=INDEX_NAME)
            logger.info("es_index_dropped", extra={"index": INDEX_NAME})
            return True
    except Exception as e:
        logger.error("es_index_drop_failed", extra={"err": str(e)})
    return False