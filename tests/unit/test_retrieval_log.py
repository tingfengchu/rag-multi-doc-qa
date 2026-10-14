"""模块 6：检索流水（``record`` / ``recent``）+ 与 ``chat`` 路由的衔接点。

看板的四个数字里有三个来自这张表，所以这里钉的是**数据怎么进去、怎么读出来**：
写失败绝不能影响问答（旁路数据）、一次检索里同一个文件只算一次命中、
命中语义缓存的那次不记（否则"被检索最多的文档"会变成"缓存命中最多的文档"）。
"""
import inspect

from api.routers import chat as chat_router
from api.schemas import ChatIn
from services import feedback_service, retrieval_log_service


class _FakeCursor:
    def __init__(self, log, results):
        self.log, self.results = log, results
        self.rowcount = 1
        self.lastrowid = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


class _FakeConn:
    def __init__(self, log, results):
        self.log, self.results = log, results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, dictionary=False):
        return _FakeCursor(self.log, self.results)


def _record(**overrides):
    kwargs = {
        "user_uuid": "uuid-alice",
        "scope": "both",
        "question": "张嘉亮是谁？",
        "rewritten_question": "张嘉亮是谁？",
        "hit_file_uuids": ["u-1", "u-2"],
        "hit_count": 5,
        "max_score": 0.83,
        "no_result": False,
    }
    kwargs.update(overrides)
    return retrieval_log_service.record(**kwargs)


class TestRecord:
    def test_stores_deduped_uuids_and_the_no_result_flag(self, monkeypatch):
        log, results = [], []
        monkeypatch.setattr(retrieval_log_service, "get_conn", lambda: _FakeConn(log, results))

        assert _record(hit_file_uuids=["u-1", "u-1", "u-2"], no_result=True) is True

        sql, params = log[0]
        assert sql.startswith("INSERT INTO retrieval_log")
        assert len(params) == 8
        assert params[6] == 1, "no_result 落库是 0/1"
        # 同一个文件在一次检索里被多个块命中只算一次（否则"被检索最多"会变成"块最多"）
        assert params[7] == '["u-1", "u-2"]'

    def test_missing_score_is_stored_as_null(self, monkeypatch):
        log, results = [], []
        monkeypatch.setattr(retrieval_log_service, "get_conn", lambda: _FakeConn(log, results))

        _record(max_score=None)

        assert log[0][1][5] is None, "没有分数就是 NULL：别编一个 0 出来"

    def test_never_raises_when_the_database_is_down(self, monkeypatch):
        """写流水是旁路：MySQL 抖一下不能毁掉一次问答（否则用户会看到"提问失败"）。"""

        def _boom():
            raise RuntimeError("mysql down")

        monkeypatch.setattr(retrieval_log_service, "get_conn", _boom)

        assert _record() is False

    def test_uuids_are_capped(self, monkeypatch):
        """上限防的是"检索层将来把整个库都召回"时把 JSON 列撑爆。"""
        log, results = [], []
        monkeypatch.setattr(retrieval_log_service, "get_conn", lambda: _FakeConn(log, results))

        many = [f"u-{i}" for i in range(retrieval_log_service.MAX_STORED_FILE_UUIDS + 20)]
        _record(hit_file_uuids=many, hit_count=len(many))

        stored = log[0][1][7]
        assert stored.count("u-") == retrieval_log_service.MAX_STORED_FILE_UUIDS


class TestRecent:
    def test_parses_json_and_tolerates_dirty_rows(self, monkeypatch):
        """脏数据（手工改过 / 写入中断）按"没有命中"处理，不能把整页统计带崩。"""
        rows = [
            {"question": "q1", "file_uuids": '["u-1"]'},
            {"question": "q2", "file_uuids": "not json"},
            {"question": "q3", "file_uuids": None},
            {"question": "q4", "file_uuids": ["u-2", 5]},
            {"question": "q5", "file_uuids": [1, 2]},
        ]
        log, results = [], [rows]
        monkeypatch.setattr(retrieval_log_service, "get_conn", lambda: _FakeConn(log, results))

        parsed = retrieval_log_service.recent(100)

        assert [row["file_uuids"] for row in parsed] == [["u-1"], [], [], ["u-2"], []]
        sql, params = log[0]
        assert "ORDER BY id DESC LIMIT %s" in sql
        assert params == (100,)

    def test_window_query_has_no_join(self, monkeypatch):
        log, results = [], [[]]
        monkeypatch.setattr(retrieval_log_service, "get_conn", lambda: _FakeConn(log, results))

        retrieval_log_service.recent(10)

        assert "JOIN" not in log[0][0].upper(), (
            "retrieval_log 与 file_dedup 的排序规则在交付机上不同，JOIN 会 1267"
        )


class TestChatWiring:
    """``chat`` 路由的两个衔接点——看板有没有数据，取决于它们。"""

    def test_done_event_carries_the_question_hash(self):
        """``done.q_hash`` 与 ``POST /feedback`` 算出的哈希必须同源。

        不同源的表现是"反馈提交成功了，但按钮还是能点，点了又插一行"——
        前端拿到的 key 与后端判重用的 key 对不上，而两边都不报错。
        """
        payload = ChatIn(question="  张嘉亮是谁？  ")

        data = chat_router._done_payload(
            payload, "public", "张嘉亮是谁？", "答案", [], False, 0.9, False, scopes=["public"]
        )

        assert data["q_hash"] == feedback_service.question_hash("张嘉亮是谁？")
        assert len(data["q_hash"]) == 40

    def test_retrieved_uuids_are_deduped_and_keep_order(self):
        def doc(meta):
            return type("D", (), {"metadata": meta})()

        docs = [
            doc({"file_uuid": "u-1"}),
            doc({}),
            doc({"file_uuid": "u-2"}),
            doc({"file_uuid": "u-1"}),
        ]

        assert chat_router._retrieved_file_uuids(docs) == ["u-1", "u-2"]

    def test_log_helper_passes_the_raw_score(self, monkeypatch):
        """流水里存的是**原始分**：0.42 与"没做分数门控"必须分得开（排障全靠它）。"""
        calls = []
        monkeypatch.setattr(
            chat_router.retrieval_log_service, "record", lambda **kw: calls.append(kw) or True
        )
        docs = [type("D", (), {"metadata": {"file_uuid": "u-1"}})()]

        chat_router._log_retrieval(
            ChatIn(question="q"), {"user_uuid": "uuid-alice"}, "both", "q", docs, 0.42, True
        )

        assert calls == [
            {
                "user_uuid": "uuid-alice",
                "scope": "both",
                "question": "q",
                "rewritten_question": "q",
                "hit_file_uuids": ["u-1"],
                "hit_count": 1,
                "max_score": 0.42,
                "no_result": True,
            }
        ]

    def test_cache_hit_branch_returns_before_logging(self):
        """命中语义缓存那条分支必须在 ``_log_retrieval`` **之前** return。

        为什么这里用源码断言：真跑一次 SSE 流要拉起向量库与 LLM（离线跑不了），
        而这条约束一旦破了，"被检索最多的文档"会变成"缓存命中最多的文档"——
        数字看着正常、含义已经错了，靠肉眼发现不了（README 的运营看板一节有说明）。
        """
        src = inspect.getsource(chat_router._sse_stream)

        assert "chat_cache_hit" in src
        assert src.index("chat_cache_hit") < src.index("_log_retrieval("), (
            "缓存命中的分支要提前 return，不能走到记流水那一步"
        )
        # 两条真检索路径（没结果 / 正常回答）都要记
        assert src.count("_log_retrieval(") == 2

