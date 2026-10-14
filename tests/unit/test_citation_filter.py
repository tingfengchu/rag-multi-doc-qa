"""引用噪声过滤（P0）：低于 ``CITATION_THRESHOLD`` 的块不许进 citations（离线、端点级）。

用户实测（2026-10-13）：问"张嘉亮是谁？"返回 5 条引用，其中 4 条是无关内容
（``自动驾驶算法.pdf``、以及 ``SAGE AI 智能问答助手.pdf`` 的第 18/4/12 页）——
它们只是 RRF 捞上来的陪跑，分数接近 0，却因为**当时没有任何引用级过滤**而全部显示。

为什么必须在**端点级**测（而不是只测 ``_build_citations_and_debug``）：
用户明确问了"是不是过滤了但分数没传进 done 事件"——只测函数抓不到这类事故。
这里把 SSE 帧解出来断言，等于沿着用户真正走的那条路验一遍。

离线策略与 ``test_chat_scope_modes.py`` 一致：只把重活换成替身
（Chroma / 检索 / LLM / Redis），**路由、SSE 编码、citations 组装全走真实代码**；
``TestClient`` 不加 ``with``，所以不触发 lifespan、不连 ES。
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

#: 用户报的那两个"陪跑"文件（分数接近 0）
NOISE = ("自动驾驶算法与嵌入式具身智能运控.pdf", "SAGE AI 智能问答助手.pdf")


def _doc(source, page, score, scope=PUBLIC, content="内容", file_uuid=None):
    return Document(
        page_content=content,
        metadata={
            "source": source,
            "page": page,
            "scope": scope,
            "rerank_score": score,
            # 溯源弹窗要靠它定位原件（下载接口只认 file_uuid）。真实检索里这个字段
            # 来自 ES/Chroma 的块元数据（见 files._index_to_es）；老文档可能没有 → None。
            "file_uuid": file_uuid,
        },
    )


#: 3 条像样的证据 + 2 条噪声（分数与用户实测的"接近 0"一致）
FIVE_DOCS = [
    _doc("我的简历 (AI应用开发)(3).pdf", 0, 0.9142),
    _doc(NOISE[1], 2, 0.3333),   # 低于阈值 → 不该被引用
    _doc("我的简历 (AI应用开发)(3).pdf", 1, 0.8021),
    _doc(NOISE[0], 0, 0.0042),   # 用户看到的那条噪声
    _doc("我的简历 (AI应用开发)(3).pdf", 2, 0.6103),
]


class Doubles:
    """记录替身调用；``docs`` 决定这次"检索"返回什么。"""

    def __init__(self, docs, store_docs=True):
        self.docs = docs
        self.store_docs = store_docs

    def retrieve(self, query, scopes, vectorstores, **kwargs):
        return list(self.docs)


class FakeStore:
    def __init__(self, has_docs):
        self._docs = [{"page_content": "x"}] if has_docs else []

    def get(self):
        return {"documents": self._docs}


class FakeVectorService:
    def get_vectorstore(self, scope):
        return FakeStore(True)


@pytest.fixture()
def doubles(monkeypatch):
    d = Doubles(FIVE_DOCS)
    monkeypatch.setattr(chat_router, "vector_service", FakeVectorService())
    monkeypatch.setattr(chat_router.retrieve_service, "retrieve_across_scopes", d.retrieve)
    monkeypatch.setattr(
        chat_router.rag_service, "generate_answer_stream", lambda q, docs: iter(["回答"])
    )
    monkeypatch.setattr(chat_router.redis_service, "get_history", lambda user, key: [])
    monkeypatch.setattr(
        chat_router.redis_service, "get_cached_answer", lambda scope, q, filter_key="all": None
    )
    monkeypatch.setattr(chat_router.redis_service, "append_history", lambda *a, **kw: None)
    monkeypatch.setattr(chat_router.redis_service, "set_cached_answer", lambda *a, **kw: None)
    monkeypatch.setattr(chat_router.query_rewrite_service, "rewrite_query", lambda q, h: q)
    monkeypatch.setattr(chat_router.query_rewrite_service, "has_usable_history", lambda h: False)
    return d


@pytest.fixture()
def client(doubles):
    c = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: USER
    yield c
    app.dependency_overrides.clear()


def _events(text):
    out = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def _ask(client, enable_rerank=True):
    resp = client.post(
        "/api/chat/stream",
        json={
            "question": "张嘉亮是谁？",
            "scope": PUBLIC,
            "enable_rerank": enable_rerank,
            "rerank_model": "BAAI/bge-reranker-base",
        },
    )
    return resp, (_events(resp.text) if resp.status_code == 200 else [])


class TestNoisyCitationsAreFiltered:
    """用户报的场景：5 条候选里有 2 条低于阈值 → 引用列表只留 3 条。"""

    def test_done_event_carries_only_three_citations(self, client):
        _, events = _ask(client)
        citations = _by_event(events, "done")[0]["citations"]

        assert len(citations) == 3, "5 条候选里 2 条低于阈值，引用必须只剩 3 条"

    def test_citations_event_matches_done_event(self, client):
        """`citations` 帧与 `done` 里的同源：前端改用哪个都看不到噪声。"""
        _, events = _ask(client)

        assert _by_event(events, "citations")[0]["citations"] == \
            _by_event(events, "done")[0]["citations"]

    def test_reported_noise_files_are_gone(self, client):
        _, events = _ask(client)
        sources = [c["source"] for c in _by_event(events, "done")[0]["citations"]]

        assert NOISE[0] not in sources, "自动驾驶算法.pdf 又出现在引用里了"
        assert NOISE[1] not in sources, "SAGE AI 智能问答助手.pdf 的陪跑页又被引用了"

    def test_every_citation_carries_a_score_above_threshold(self, client):
        """用户追问的"是不是过滤了但分数没传进来"：每条引用都必须带分数且过线。"""
        _, events = _ask(client)

        for citation in _by_event(events, "done")[0]["citations"]:
            assert isinstance(citation["rerank_score"], float)
            assert citation["rerank_score"] >= chat_router.CITATION_THRESHOLD

    def test_raising_citation_threshold_does_not_move_the_refusal_line(self, client, monkeypatch):
        """把引用阈值调高**不许**波及拒答线——这正是"合用一个常量"会踩的坑。"""
        monkeypatch.setattr(chat_router, "CITATION_THRESHOLD", 0.95)
        _, events = _ask(client)
        done = _by_event(events, "done")[0]

        assert done["citations"] == []       # 引用收紧到 0.95 → 一条都不剩
        assert done["refused"] is False      # 但最高分 0.9142 仍 ≥ 拒答线 0.5
        assert "未找到" not in done["answer"]


class TestDebugStillShowsFilteredBlocks:
    """被滤掉的块要留在 debug 事件里：排障靠它回答"为什么这条没进引用"。"""

    def test_debug_keeps_all_candidates_with_scores_and_cited_flag(self, client):
        _, events = _ask(client)
        debug = _by_event(events, "debug")[0]["debug_info"]

        assert len(debug) == len(FIVE_DOCS), "调试信息不该跟着引用一起被滤掉"
        assert [item["cited"] for item in debug] == [True, False, True, False, True]
        assert debug[3]["rerank_score"] == pytest.approx(0.0042)


class TestAllNoiseMeansRefusal:
    """全部候选都低于阈值 → 走拒答路径，且**不给任何引用**。"""

    @pytest.fixture()
    def noisy(self, doubles):
        doubles.docs = [_doc(NOISE[0], 0, 0.11), _doc(NOISE[1], 3, 0.0042)]
        return doubles

    def test_refusal_without_citations(self, client, noisy):
        _, events = _ask(client)
        done = _by_event(events, "done")[0]

        assert done["refused"] is True
        assert done["citations"] == []
        assert _by_event(events, "citations") == [], "拒答还带引用 = 用户又看到噪声"
        assert "未在知识库中找到足够相关的信息" in done["answer"]

    def test_refusal_reports_the_real_max_score(self, client, noisy):
        _, events = _ask(client)

        assert _by_event(events, "done")[0]["max_score"] == pytest.approx(0.11)


class TestDedupKeepsTheBestScore:
    """同一 (文件, 页, 库) 被多个子块命中：留最高分，低分那次不许"占坑"。"""

    def test_low_score_duplicate_does_not_block_a_high_score_citation(self, client, doubles):
        doubles.docs = [
            _doc("a.pdf", 0, 0.20),   # 先到，但分数不够 → 先被阈值滤掉
            _doc("a.pdf", 0, 0.80),   # 后到那次命中有 0.8 → 应该被引用
        ]
        _, events = _ask(client)
        citations = _by_event(events, "done")[0]["citations"]

        assert len(citations) == 1
        assert citations[0]["rerank_score"] == pytest.approx(0.8)

    def test_duplicate_keeps_the_highest_score(self, client, doubles):
        doubles.docs = [_doc("a.pdf", 0, 0.55), _doc("a.pdf", 0, 0.93)]
        _, events = _ask(client)
        citations = _by_event(events, "done")[0]["citations"]

        assert [c["rerank_score"] for c in citations] == [pytest.approx(0.93)]

    def test_missing_score_field_uses_the_sentinel(self, client, doubles):
        """老文档缺 `rerank_score` 字段 → 兜底哨兵 1.0（语义仍是"不做门控"），不被误滤。"""
        doubles.docs = [Document(page_content="x", metadata={"source": "old.pdf", "page": 0})]
        _, events = _ask(client)

        assert len(_by_event(events, "done")[0]["citations"]) == 1


class TestSentinelScoreIsNotAConfidence:
    """关闭精排时检索层写哨兵 1.0（"这次不做分数门控"）——它不是置信度，不许当 1.00 显示。"""

    @pytest.fixture()
    def sentinel(self, doubles):
        doubles.docs = [_doc(NOISE[0], 0, 1.0), _doc(NOISE[1], 1, 1.0)]
        return doubles

    def test_citations_still_shown_but_no_confidence_number(self, client, sentinel):
        _, events = _ask(client, enable_rerank=False)
        done = _by_event(events, "done")[0]

        assert len(done["citations"]) == 2, "关掉精排不等于关掉引用（那是另一个开关）"
        assert done["max_score"] is None, "哨兵 1.0 被当置信度发出去了"

    def test_real_score_is_reported_when_rerank_is_on(self, client):
        _, events = _ask(client, enable_rerank=True)

        assert _by_event(events, "done")[0]["max_score"] == pytest.approx(0.9142)


class TestConfidenceTextNeverPrintsOnePointZeroZero:
    """1.00 是哨兵值：文案层不许把 0.999 舍入成 "1.00"（前端 formatConfidence 同口径）。"""

    @pytest.mark.parametrize(
        "value,expected",
        [(0.999, "0.99"), (0.996, "0.99"), (0.62, "0.62"), (0.0042, "0.00")],
    )
    def test_format_confidence(self, value, expected):
        assert chat_router._format_confidence(value) == expected

    @staticmethod
    def _payload(enable_rerank: bool):
        from api.schemas import ChatIn

        return ChatIn(question="q", scope=PUBLIC, enable_rerank=enable_rerank)

    def test_real_score_is_reported(self):
        assert chat_router._display_score(self._payload(True), 0.9142) == pytest.approx(0.9142)

    def test_sentinel_is_reported_as_unknown(self):
        assert chat_router._display_score(self._payload(True), 1.0) is None

    def test_rerank_disabled_reports_no_confidence_at_all(self):
        assert chat_router._display_score(self._payload(False), 0.9142) is None

    def test_missing_score_is_reported_as_unknown(self):
        assert chat_router._display_score(self._payload(True), None) is None

class TestCitationCarriesTraceInfo:
    """引用要带 ``file_uuid`` + ``excerpt``：没有它们，溯源弹窗只能重复"文件名 + 第 N 页"，
    用户还得自己翻到那一页去找（这正是"引用可点击"要做的事）。"""

    @pytest.fixture()
    def traceable(self, doubles):
        doubles.docs = [
            _doc("SAGE AI 智能问答助手.pdf", 11, 0.82, content="第一部分  产品定位\n\n本文档介绍…",
                 file_uuid="4f66d10f-0754-44ef-ba80-630e0bed1d53"),
        ]
        return doubles

    def test_citation_carries_file_uuid_and_excerpt(self, client, traceable):
        _, events = _ask(client)
        citation = _by_event(events, "done")[0]["citations"][0]

        assert citation["file_uuid"] == "4f66d10f-0754-44ef-ba80-630e0bed1d53"
        assert citation["excerpt"] == "第一部分 产品定位 本文档介绍…", "摘录必须压掉多余空白"

    def test_long_excerpt_is_truncated(self, client, doubles):
        doubles.docs = [_doc("长文.pdf", 0, 0.9, content="正" * (chat_router.EXCERPT_CHARS + 50))]
        _, events = _ask(client)
        excerpt = _by_event(events, "done")[0]["citations"][0]["excerpt"]

        assert len(excerpt) == chat_router.EXCERPT_CHARS + 1     # 截断后的内容 + 省略号
        assert excerpt.endswith("…")

    def test_excerpt_follows_the_best_matching_chunk(self, client, doubles):
        """去重后分数取最高那次 → 摘录也必须换成那一次的内容，不许"高分 + 差摘录"自相矛盾。"""
        doubles.docs = [
            _doc("a.pdf", 0, 0.55, content="低分那次的内容", file_uuid="u1"),
            _doc("a.pdf", 0, 0.93, content="高分那次的内容", file_uuid="u1"),
        ]
        _, events = _ask(client)
        citation = _by_event(events, "done")[0]["citations"][0]

        assert citation["rerank_score"] == pytest.approx(0.93)
        assert citation["excerpt"] == "高分那次的内容"

    def test_old_document_without_file_uuid_is_honest(self, client, doubles):
        """老文档没有 file_uuid → 发 ``None``（前端据此禁用下载并说明原因），不编造 uuid。"""
        doubles.docs = [Document(page_content="x", metadata={"source": "old.pdf", "page": 0})]
        _, events = _ask(client)
        citation = _by_event(events, "done")[0]["citations"][0]

        assert citation["file_uuid"] is None
        assert citation["excerpt"] == "x"


def _by_event(events, name):
    return [data for event, data in events if event == name]
