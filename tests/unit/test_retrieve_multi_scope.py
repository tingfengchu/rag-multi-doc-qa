"""``retrieve_service.retrieve_across_scopes`` 的多库合并（模块 4，离线、不连 ES/Chroma）。

锁定的行为（每一条都对应一种"看起来只是少搜了点东西"的静默故障）：

1. 每个库**各跑一次** BM25 + 向量召回，且各自带自己的 scope —— 多租户隔离不能被
   "先合并再过滤"绕过；
2. 合并后**全局精排一次**（不是每库各自精排再拼）：精排会做 min-max/sigmoid 归一化，
   分库精排会让"公共库第 3 名"与"私人库第 1 名"的分数不可比；
3. 每个块的 ``metadata["scope"]`` 是可信的库归属（前端引用标注「公共库 / 我的文档」靠它）；
4. 同一份内容同时存在于两个库时**两条都留**（去重键带 scope）——合并掉就等于把
   "这条证据从哪来"丢了；
5. 父块回溯后仍保留库归属（否则引用又会退回"未知库"）；
6. 没文档的库被跳过，不会被查询（Chroma 空集合在部分版本上会抛底层错误，
   见 README"已知限制"）。
"""
import pytest
from langchain_core.documents import Document

from services import retrieve_service

PUBLIC = "public"
PRIVATE = "private:uuid-alice"


def _hit(content, scope, parent_id=None, page=0):
    """构造一条 ES ``_source``（结构与 ``es_service.search_bm25`` 的返回一致）。"""
    src = {
        "content": content,
        "metadata": {"scope": scope, "source": f"{scope}__手册.pdf", "page": page},
    }
    if parent_id:
        src["parent_id"] = parent_id
    return {"_source": src}


class FakeEs:
    """``services.es_service`` 的替身：BM25 按 scope 返回预设命中，父块走查表。"""

    def __init__(self, hits_by_scope=None, parents=None):
        self.hits = hits_by_scope or {}
        self.parents = parents or {}
        self.bm25_calls = []
        self.parent_calls = []

    def search_bm25(self, query, scope, top_k=10, file_uuid_filter=None):
        self.bm25_calls.append(
            {"query": query, "scope": scope, "top_k": top_k, "filter": file_uuid_filter}
        )
        return self.hits.get(scope, [])

    def get_parent_chunk(self, parent_id):
        self.parent_calls.append(parent_id)
        return self.parents.get(parent_id)


class FakeVectorStore:
    """Chroma 替身：记录检索调用，返回预设块。"""

    def __init__(self, docs=None):
        self.docs = docs or []
        self.calls = []

    def similarity_search(self, query, k, filter=None):
        self.calls.append({"query": query, "k": k, "filter": filter})
        return list(self.docs)


@pytest.fixture()
def fake_es(monkeypatch):
    fake = FakeEs()
    monkeypatch.setattr(retrieve_service, "es_service", fake)
    return fake


class CountingCrossEncoder:
    """精排模型替身：记录被喂进来的 pair 数，并按给定 logits 返回分数。"""

    def __init__(self, logits_by_text=None):
        self.logits_by_text = logits_by_text or {}
        self.calls = []

    def score(self, pairs):
        self.calls.append(len(pairs))
        return [self.logits_by_text.get(text, 0.0) for _, text in pairs]


@pytest.fixture()
def fake_encoder(monkeypatch):
    """把 ``get_cross_encoder_model`` 换成"每次返回同一个替身"，便于统计调用次数。"""
    box = {"encoder": None}

    def fake_getter(model_name):
        box["encoder"] = box["encoder"] or CountingCrossEncoder()
        return box["encoder"]

    monkeypatch.setattr(retrieve_service, "get_cross_encoder_model", fake_getter)
    return box


def _stores(public_docs=None, private_docs=None, scopes=(PUBLIC, PRIVATE)):
    """按 scope 组装 vectorstores 字典（只放"有文档"的库）。"""
    stores = {}
    if PUBLIC in scopes:
        stores[PUBLIC] = FakeVectorStore(public_docs)
    if PRIVATE in scopes:
        stores[PRIVATE] = FakeVectorStore(private_docs)
    return stores


class TestPerScopeRecall:
    def test_bm25_is_called_once_per_scope_with_its_own_scope(self, fake_es):
        """每个库都要有一次带**自己** scope 的 BM25 调用（隔离不能靠后置过滤）。"""
        stores = _stores([], [])
        retrieve_service.retrieve_across_scopes(
            "年假", [PUBLIC, PRIVATE], stores, enable_rerank=False
        )

        assert [c["scope"] for c in fake_es.bm25_calls] == [PUBLIC, PRIVATE]
        assert all(c["query"] == "年假" for c in fake_es.bm25_calls)

    def test_vector_search_carries_file_uuid_filter(self, fake_es):
        stores = _stores([], [])
        retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=False, file_uuid_filter="file-9"
        )

        for scope in (PUBLIC, PRIVATE):
            assert stores[scope].calls[0]["filter"] == {"file_uuid": "file-9"}

    def test_library_without_vectorstore_is_skipped(self, fake_es):
        """空库不在字典里 → 既不查 Chroma 也不查 BM25（空集合查询会炸，见模块说明）。"""
        stores = _stores(public_docs=[], scopes=(PUBLIC,))
        retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=False
        )

        assert [c["scope"] for c in fake_es.bm25_calls] == [PUBLIC]


class TestScopeTagging:
    def test_every_doc_is_tagged_with_its_library(self, fake_es):
        fake_es.hits = {PUBLIC: [_hit("公共库内容", PUBLIC)], PRIVATE: [_hit("私人库内容", PRIVATE)]}
        stores = _stores([], [])

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=False
        )

        assert {d.metadata["scope"] for d in docs} == {PUBLIC, PRIVATE}
        assert {d.page_content for d in docs} == {"公共库内容", "私人库内容"}

    def test_vector_docs_without_scope_metadata_are_tagged_too(self, fake_es):
        """Chroma 侧 metadata 不保证带 scope（历史数据可能没有）→ 仍必须打上库标。"""
        public_doc = Document(page_content="向量块", metadata={"source": "x.pdf", "page": 0})
        stores = _stores(public_docs=[public_doc], scopes=(PUBLIC,))

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC], stores, enable_rerank=False
        )

        assert docs[0].metadata["scope"] == PUBLIC


class TestDedupe:
    def test_same_content_in_two_libraries_is_kept_twice(self, fake_es):
        """同一份文档同时存在于公共库与私人库时，两条都要留（各自的库标不同）。"""
        fake_es.hits = {PUBLIC: [_hit("同样的内容", PUBLIC)], PRIVATE: [_hit("同样的内容", PRIVATE)]}
        stores = _stores([], [])

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=False
        )

        assert len(docs) == 2
        assert {d.metadata["scope"] for d in docs} == {PUBLIC, PRIVATE}

    def test_same_content_twice_in_one_library_is_deduped(self, fake_es):
        """同一个库内、两个召回臂命中同一块 → 只留一条（RRF 融合的既有语义）。"""
        fake_es.hits = {PUBLIC: [_hit("重复内容", PUBLIC)]}
        stores = _stores(
            public_docs=[Document(page_content="重复内容", metadata={"page": 0})],
            scopes=(PUBLIC,),
        )

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC], stores, enable_rerank=False
        )

        assert len(docs) == 1


class TestGlobalRerank:
    def test_rerank_runs_once_over_all_candidates(self, fake_es, fake_encoder):
        """两个库的候选必须**一次性**喂给精排（分库精排会让跨库分数不可比）。"""
        fake_es.hits = {
            PUBLIC: [_hit("公共 A", PUBLIC), _hit("公共 B", PUBLIC)],
            PRIVATE: [_hit("私人 A", PRIVATE)],
        }
        stores = _stores([], [])

        retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=True
        )

        assert fake_encoder["encoder"].calls == [3]

    def test_stronger_private_hit_can_outrank_public_hit(self, fake_es, fake_encoder):
        """全局精排的意义：私人库里的强证据要能排到公共库前面，而不是按库轮流取。"""
        fake_encoder["encoder"] = CountingCrossEncoder({"公共 A": -5.0, "私人 A": 5.0})
        fake_es.hits = {PUBLIC: [_hit("公共 A", PUBLIC)], PRIVATE: [_hit("私人 A", PRIVATE)]}
        stores = _stores([], [])

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=True
        )

        assert docs[0].metadata["scope"] == PRIVATE
        assert docs[0].metadata["rerank_score"] > docs[1].metadata["rerank_score"]

    def test_saturated_scores_keep_rrf_order(self, fake_es, fake_encoder):
        """分数没有区分度（英文模型跑中文）时保留 RRF 顺序：首条仍是先出现的公共库块。"""
        fake_encoder["encoder"] = CountingCrossEncoder({"公共 A": 9.5, "私人 A": 9.5001})
        fake_es.hits = {PUBLIC: [_hit("公共 A", PUBLIC)], PRIVATE: [_hit("私人 A", PRIVATE)]}
        stores = _stores([], [])

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=True
        )

        assert docs[0].page_content == "公共 A"


class TestParentExpansion:
    def test_parent_keeps_library_tag(self, fake_es):
        """父块回溯不能把库标弄丢——前端引用标注就靠它。"""
        fake_es.hits = {PRIVATE: [_hit("子块", PRIVATE, parent_id="p-1")]}
        fake_es.parents = {
            "p-1": {"content": "完整父块", "metadata": {"source": "私人__.pdf", "page": 0}}
        }
        stores = _stores(public_docs=[], scopes=(PRIVATE,))

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PRIVATE], stores, enable_rerank=False
        )

        assert docs[0].page_content == "完整父块"
        assert docs[0].metadata["scope"] == PRIVATE
        assert fake_es.parent_calls == ["p-1"]


class TestSingleScopeEquivalence:
    def test_single_scope_matches_legacy_function(self, fake_es, fake_encoder):
        """单库走新函数的结果必须与老函数一致（否则等于顺手改了线上检索行为）。"""
        hits = {PUBLIC: [_hit("A", PUBLIC), _hit("B", PUBLIC)]}
        fake_encoder["encoder"] = CountingCrossEncoder({"A": 3.0, "B": -3.0})

        fake_es.hits = hits
        legacy = retrieve_service.get_hybrid_retriever_with_es(
            "q", PUBLIC, FakeVectorStore([]), enable_rerank=True
        )

        fake_es.hits = hits
        fake_es.bm25_calls.clear()
        new = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC], _stores(public_docs=[], scopes=(PUBLIC,)), enable_rerank=True
        )

        assert [d.page_content for d in new] == [d.page_content for d in legacy]

    def test_result_is_capped_at_top_n(self, fake_es):
        """无论查几个库，交给 LLM 的上下文条数上限不变（TOP_N_FOR_CONTEXT）。"""
        fake_es.hits = {
            PUBLIC: [_hit(f"p{i}", PUBLIC) for i in range(8)],
            PRIVATE: [_hit(f"s{i}", PRIVATE) for i in range(8)],
        }
        stores = _stores([], [])

        docs = retrieve_service.retrieve_across_scopes(
            "q", [PUBLIC, PRIVATE], stores, enable_rerank=False
        )

        assert len(docs) == retrieve_service.TOP_N_FOR_CONTEXT
