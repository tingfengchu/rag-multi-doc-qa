"""模块 4：三种检索范围（``public`` / ``private:<uuid>`` / ``both``）的端到端路径（离线）。

为什么必须在**端点级**测而不是只测 ``resolve_scope_set``：
    函数级矩阵只能证明"scope 解析对了"，证明不了"解析结果真的被检索层用了"。
    真正会上线的两类事故它都测不到：
    ① 路由忘了把列表传下去（both 模式实际只检了一个库，用户以为"没搜到"）；
    ② 检索层用错了 scope（查了公共库却把引用标成"我的文档"）。

离线策略与 ``test_upload_scope.py`` 一致：只把重活换成替身
（Chroma / ES / Redis / LLM / 精排模型），**路由、依赖注入、SSE 编码、scope 解析
全部走真实代码**；``TestClient`` 不加 ``with``，所以不触发 lifespan、不连 ES。
"""
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from api.deps import get_current_user
from api.main import app
from api.routers import chat as chat_router

USER = {"user_uuid": "uuid-alice", "username": "alice", "role": "user"}
PUBLIC = "public"
PRIVATE = "private:uuid-alice"


class FakeStore:
    """Chroma 向量库替身：``.get()`` 返回块列表，空库 = 没有 documents。"""

    def __init__(self, has_docs: bool):
        self._docs = [{"page_content": "x"}] if has_docs else []

    def get(self):
        return {"documents": self._docs}


class FakeVectorService:
    def __init__(self, non_empty_scopes):
        self.non_empty = set(non_empty_scopes)

    def get_vectorstore(self, scope):
        return FakeStore(scope in self.non_empty)


class Doubles:
    """记录替身调用，供断言"哪个库被真的检索了"。"""

    def __init__(self):
        self.retrieve_calls = []
        self.history_keys = []
        self.cached_keys = []

    def retrieve(self, query, scopes, vectorstores, **kwargs):
        self.retrieve_calls.append({"query": query, "scopes": list(scopes)})
        # 每个被检索的库各返回一块，metadata 带 scope——与真实检索层一致
        # （retrieve_across_scopes 会给每个块显式打 scope 标）
        return [
            Document(
                page_content=f"来自 {scope} 的内容",
                metadata={
                    "source": f"{scope}__手册.pdf",
                    "page": 0,
                    "scope": scope,
                    "rerank_score": 0.9,
                },
            )
            for scope in scopes
        ]


@pytest.fixture()
def doubles(monkeypatch):
    d = Doubles()

    monkeypatch.setattr(chat_router, "vector_service", FakeVectorService({PUBLIC, PRIVATE}))
    monkeypatch.setattr(chat_router.retrieve_service, "retrieve_across_scopes", d.retrieve)
    monkeypatch.setattr(
        chat_router.rag_service, "generate_answer_stream", lambda q, docs: iter(["回答"])
    )

    monkeypatch.setattr(
        chat_router.redis_service,
        "get_history",
        lambda user, key: d.history_keys.append(key) or [],
    )
    monkeypatch.setattr(
        chat_router.redis_service,
        "get_cached_answer",
        lambda scope, q, filter_key="all": d.cached_keys.append(scope) or None,
    )
    monkeypatch.setattr(chat_router.redis_service, "append_history", lambda *a, **kw: None)
    monkeypatch.setattr(chat_router.redis_service, "set_cached_answer", lambda *a, **kw: None)

    monkeypatch.setattr(chat_router.query_rewrite_service, "rewrite_query", lambda q, h: q)
    monkeypatch.setattr(chat_router.query_rewrite_service, "has_usable_history", lambda h: False)
    return d


@pytest.fixture()
def client(doubles):
    """不带 ``with`` 的 TestClient：不跑 lifespan（本文件不连 ES/Redis/MySQL）。"""
    c = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: USER
    yield c
    app.dependency_overrides.clear()


def _events(text):
    """把 SSE 响应体解成 ``[(event, data), ...]``（帧格式由 test_sse.py 单独锁定）。"""
    out = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def _ask(client, scope):
    resp = client.post(
        "/api/chat/stream",
        json={"question": "年假几天？", "scope": scope, "enable_rerank": False},
    )
    return resp, (_events(resp.text) if resp.status_code == 200 else [])


def _by_event(events, name):
    return [data for event, data in events if event == name]


class TestScopeRouting:
    """三种 scope 各自应该检索哪些库（模块 4 的核心契约）。"""

    def test_public_only_searches_public(self, client, doubles):
        resp, _ = _ask(client, PUBLIC)

        assert resp.status_code == 200
        assert doubles.retrieve_calls == [{"query": "年假几天？", "scopes": [PUBLIC]}]

    def test_private_only_searches_private(self, client, doubles):
        _, _ = _ask(client, PRIVATE)

        assert doubles.retrieve_calls[0]["scopes"] == [PRIVATE]

    def test_both_searches_public_then_private(self, client, doubles):
        """both 必须真的检两个库，且顺序是 public → private（缓存 key 也按这个顺序）。"""
        _, _ = _ask(client, "both")

        assert doubles.retrieve_calls[0]["scopes"] == [PUBLIC, PRIVATE]

    def test_scope_key_is_stable_across_history_and_cache(self, client, doubles):
        """历史、语义缓存、done 事件必须用**同一个** key，否则 both 模式会"答完就失忆"。"""
        _, events = _ask(client, "both")
        key = "public+private:uuid-alice"

        assert doubles.history_keys == [key]
        assert doubles.cached_keys == [key]
        assert _by_event(events, "done")[0]["scope"] == key

    def test_public_mode_keeps_legacy_key(self, client, doubles):
        """单库模式的 key 必须与模块 4 之前**完全相同**（老缓存 / 老历史不失效）。"""
        _, _ = _ask(client, PUBLIC)

        assert doubles.history_keys == [PUBLIC]
        assert doubles.cached_keys == [PUBLIC]


class TestMetaEvent:
    def test_meta_reports_mode_and_searched_scopes(self, client):
        _, events = _ask(client, "both")
        meta = _by_event(events, "meta")[0]

        assert meta["scope"] == "public+private:uuid-alice"
        assert meta["scopes"] == [PUBLIC, PRIVATE]
        assert meta["skipped_scopes"] == []
        assert meta["rewrite_skipped"] == "no_history"

    def test_empty_private_library_is_skipped_not_fatal(self, client, monkeypatch):
        """私人库还是空的（新用户最常见状态）→ both 仍要能答，只跳过空库。"""
        monkeypatch.setattr(chat_router, "vector_service", FakeVectorService({PUBLIC}))
        _, events = _ask(client, "both")
        meta = _by_event(events, "meta")[0]

        assert meta["scopes"] == [PUBLIC]
        assert meta["skipped_scopes"] == [PRIVATE]
        assert _by_event(events, "error") == []
        assert _by_event(events, "done")[0]["scopes"] == [PUBLIC]

    def test_all_libraries_empty_is_a_clear_error(self, client, monkeypatch):
        """全部为空时给可读的中文报错（点名是哪两个库），而不是空检索跑到 LLM。"""
        monkeypatch.setattr(chat_router, "vector_service", FakeVectorService(set()))
        _, events = _ask(client, "both")

        message = _by_event(events, "error")[0]["message"]
        assert "公共库" in message and "我的文档" in message


class TestCitations:
    def test_citation_carries_scope_for_each_library(self, client):
        """引用要能标出"这条证据来自哪个库"：两个库各一条，不能合并成一条。"""
        _, events = _ask(client, "both")
        citations = _by_event(events, "citations")[0]["citations"]

        assert [c["scope"] for c in citations] == [PUBLIC, PRIVATE]
        # 展示名会剥掉 "public__" 前缀（同一份文件在两个库里时，靠 scope 区分归属）
        assert citations[0]["source"] == "手册.pdf"

    def test_single_scope_citation_still_tagged(self, client):
        """单库模式即使上游 metadata 没带 scope，也要用本次唯一的 scope 兜底打标。"""
        _, events = _ask(client, PRIVATE)
        citations = _by_event(events, "citations")[0]["citations"]

        assert citations[0]["scope"] == PRIVATE


class TestPermissionStillEnforced:
    """模块 4 不能成为越权入口：新增模式一律经过 ``resolve_scope_set``。"""

    def test_other_users_private_is_forbidden(self, client):
        resp = client.post(
            "/api/chat/stream", json={"question": "q", "scope": "private:uuid-bob"}
        )

        assert resp.status_code == 403
        assert "私人库" in resp.json()["detail"]

    def test_unknown_scope_is_bad_request(self, client):
        resp = client.post("/api/chat/stream", json={"question": "q", "scope": "all"})

        assert resp.status_code == 400

