"""模块 6：运营看板统计（``GET /api/admin/stats``）。

离线策略：三个数据源（检索流水 / 反馈 / 文件名）换成替身，**聚合逻辑本身是真的**
（``services/stats_service``），路由与权限也是真的。真 SQL 的形状由
``tests/unit/test_retrieval_log.py`` 与 ``test_feedback_dedupe`` 那组钉住。

为什么把"排序固定"也写进断言：看板允许并列（两个问题都问了 2 次），
排序不稳定的话，同一份数据每次刷新顺序都不同——界面上看是"列表在跳"，
而单测会变成随机失败（那种失败最后一定会被人用 ``pytest -p no:randomly`` 之类的
方式绕过去，等于没测）。
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.deps import get_current_user
from api.main import app
from api.routers import admin as admin_router
from services import feedback_service, file_state, retrieval_log_service, stats_service
from utils.roles import ROLE_ADMIN, ROLE_USER

ADMIN = {"user_uuid": "uuid-root", "username": "root", "role": ROLE_ADMIN}
USER = {"user_uuid": "uuid-alice", "username": "alice", "role": ROLE_USER}

EMPTY_SUMMARY = {
    "total": 0,
    "up": 0,
    "down": 0,
    "up_rate": None,
    "by_scope": [],
    "top_reasons": [],
}


def _log(question, *, no_result=0, uuids=(), day=1, scope="public"):
    """一行 ``retrieval_log_service.recent`` 的返回（file_uuids 已解析成 list）。"""
    return {
        "question": question,
        "scope": scope,
        "hit_count": len(uuids),
        "max_score": 0.93,
        "no_result": no_result,
        "file_uuids": list(uuids),
        "created_at": datetime(2026, 10, day, 9, 0, 0),
    }


@pytest.fixture()
def sources(monkeypatch):
    """三个数据源替身；返回可写容器，用例自己填 ``rows`` / ``names`` / ``summary``。"""
    state = {"rows": [], "names": {}, "summary": dict(EMPTY_SUMMARY), "asked": []}

    def fake_recent(limit):
        state["asked"].append(limit)
        return list(state["rows"])[:limit]

    monkeypatch.setattr(retrieval_log_service, "recent", fake_recent)
    monkeypatch.setattr(
        file_state,
        "names_for_uuids",
        lambda uuids: {uuid: state["names"].get(uuid, "") for uuid in uuids},
    )
    monkeypatch.setattr(feedback_service, "summary", lambda: dict(state["summary"]))
    return state


class TestAggregation:
    """四个块各自的算法（含"排序必须稳定"）。"""

    def test_top_questions_merges_whitespace_variants(self, sources):
        """同一个问题的不同空白写法算一条：否则"高频提问"永远排不出像样的榜。"""
        sources["rows"] = [
            _log("张嘉亮是谁？", day=3),
            _log("张嘉亮是谁？   ", day=2),
            _log("SAGE 是什么", day=1),
        ]

        top = stats_service.build_stats()["top_questions"]

        assert [(row["question"], row["count"]) for row in top] == [
            ("张嘉亮是谁？", 2),
            ("SAGE 是什么", 1),
        ]
        # 展示用**最近一次**的原文，时间也是最近那次的（列表新的在前）
        assert top[0]["last_at"] == datetime(2026, 10, 3, 9, 0, 0)

    def test_ties_are_ordered_deterministically(self, sources):
        """并列时按问题文本升序：顺序稳定，界面才不会每次刷新都在跳。"""
        sources["rows"] = [_log("b 问题", day=1), _log("a 问题", day=2)]

        first = stats_service.build_stats()["top_questions"]
        second = stats_service.build_stats()["top_questions"]

        assert [row["question"] for row in first] == ["a 问题", "b 问题"]
        assert first == second, "同一份数据两次统计必须给出同样的顺序"

    def test_no_result_queries_only_include_flagged_rows(self, sources):
        sources["rows"] = [
            _log("答得出来的问题"),
            _log("检索不到的问题", no_result=1, day=2),
            _log("检索不到的问题", no_result=1, day=3),
        ]

        data = stats_service.build_stats()

        assert [row["question"] for row in data["no_result_queries"]] == ["检索不到的问题"]
        assert data["no_result_queries"][0]["count"] == 2
        # 高频提问是"全部提问"，不受 no_result 影响
        assert len(data["top_questions"]) == 2

    def test_top_documents_counts_hits_and_resolves_names(self, sources):
        sources["rows"] = [
            _log("q1", uuids=("u-1", "u-2")),
            _log("q2", uuids=("u-1",), day=2),
            # 一个文件在一次检索里命中多个块 → 只算一次（record 已去重，这里再兜一层）
            _log("q3", uuids=("u-2", "u-2"), day=3),
        ]
        sources["names"] = {"u-1": "手册.pdf", "u-2": "自动驾驶算法.pdf"}

        docs = stats_service.build_stats()["top_documents"]

        assert docs == [
            {"file_uuid": "u-1", "file_name": "手册.pdf", "hits": 2},
            {"file_uuid": "u-2", "file_name": "自动驾驶算法.pdf", "hits": 2},
        ]

    def test_deleted_document_survives_with_an_empty_name(self, sources):
        """文件删了但统计要留着：它确实被检索过——把名字空掉，而不是把行删掉。"""
        sources["rows"] = [_log("q1", uuids=("u-gone",))]
        sources["names"] = {}

        docs = stats_service.build_stats()["top_documents"]

        assert docs == [{"file_uuid": "u-gone", "file_name": "", "hits": 1}]

    def test_summary_passes_through_and_window_is_reported(self, sources):
        sources["rows"] = [_log("q1"), _log("q2", day=2)]
        sources["summary"] = {
            "total": 3,
            "up": 2,
            "down": 1,
            "up_rate": 0.6667,
            "by_scope": [{"scope": "public", "up": 2, "down": 1}],
            "top_reasons": [{"reason": "资料缺失", "count": 1}],
        }

        data = stats_service.build_stats(window=123)

        assert data["window"] == 123
        assert data["retrieval_total"] == 2
        assert data["feedback_summary"]["up_rate"] == 0.6667
        assert sources["asked"] == [123], "窗口要如实传给数据源（不能先全量再截断）"

    def test_empty_log_returns_four_empty_blocks(self, sources):
        data = stats_service.build_stats()

        assert data["retrieval_total"] == 0
        assert data["top_questions"] == []
        assert data["no_result_queries"] == []
        assert data["top_documents"] == []
        assert data["feedback_summary"]["up_rate"] is None
        assert data["generated_at"], "快照时间必须有：看板上的数字不是实时的"



class TestEndpoint:
    """``GET /api/admin/stats``：谁能看 + 响应形状（聚合换替身，路由与权限是真的）。"""

    @pytest.fixture()
    def client(self, sources):
        c = TestClient(app)
        yield c
        app.dependency_overrides.clear()

    def test_requires_login(self, sources):
        """没有会话 → 401（刻意不加 override）。"""
        assert TestClient(app).get("/api/admin/stats").status_code == 401

    def test_plain_user_is_forbidden(self, client):
        app.dependency_overrides[get_current_user] = lambda: USER

        assert client.get("/api/admin/stats").status_code == 403

    def test_admin_gets_all_four_blocks(self, client, sources):
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        sources["rows"] = [_log("张嘉亮是谁？", uuids=("u-1",)), _log("没有答案的问题", no_result=1)]
        sources["names"] = {"u-1": "自动驾驶算法.pdf"}
        sources["summary"] = {
            "total": 2,
            "up": 1,
            "down": 1,
            "up_rate": 0.5,
            "by_scope": [{"scope": "public", "up": 1, "down": 1}],
            "top_reasons": [{"reason": "资料缺失", "count": 1}],
        }

        resp = client.get("/api/admin/stats")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {
            "generated_at",
            "window",
            "retrieval_total",
            "top_questions",
            "no_result_queries",
            "top_documents",
            "feedback_summary",
        }
        assert body["top_questions"][0]["question"] == "张嘉亮是谁？"
        assert body["no_result_queries"][0]["question"] == "没有答案的问题"
        assert body["top_documents"][0]["file_name"] == "自动驾驶算法.pdf"
        assert body["feedback_summary"]["by_scope"][0]["scope"] == "public"

    def test_window_must_be_a_sane_size(self, client):
        """``?window=1`` 那种"只看一条流水"的调用要被挡掉：看着像统计，其实是一个样本。"""
        app.dependency_overrides[get_current_user] = lambda: ADMIN

        assert client.get("/api/admin/stats", params={"window": 1}).status_code == 422
        assert client.get("/api/admin/stats", params={"window": 10_000_000}).status_code == 422
        assert client.get("/api/admin/stats", params={"window": 100}).status_code == 200

    def test_read_failure_is_500_not_an_empty_dashboard(self, client, monkeypatch):
        """统计读不出来必须报错：四个块全 0 与"数据库挂了"在界面上必须分得开。"""
        app.dependency_overrides[get_current_user] = lambda: ADMIN

        def _boom(**kwargs):
            raise RuntimeError("mysql down")

        monkeypatch.setattr(admin_router.stats_service, "build_stats", _boom)

        resp = client.get("/api/admin/stats")

        assert resp.status_code == 500
        assert "统计" in resp.json()["detail"]



class _FakeCursor:
    def __init__(self, log, results):
        self.log, self.results = log, results
        self.rowcount = 1

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


class TestFeedbackDedupe:
    """``q_hash``：同一个问题在防重窗口内只留一条反馈（挡手抖，不挡跨天的真信号）。"""

    def test_hash_ignores_whitespace_and_english_case(self):
        assert feedback_service.question_hash("张嘉亮是谁？") == feedback_service.question_hash(
            "  张嘉亮是谁？  "
        )
        assert feedback_service.question_hash("SAGE AI") == feedback_service.question_hash(
            "sage   ai"
        )
        assert len(feedback_service.question_hash("x")) == 40

    def test_different_questions_get_different_hashes(self):
        assert feedback_service.question_hash("A") != feedback_service.question_hash("B")

    def test_lookup_is_limited_to_the_dedupe_window(self, monkeypatch):
        log, results = [], [None]
        monkeypatch.setattr(feedback_service, "get_conn", lambda: _FakeConn(log, results))

        assert feedback_service.find_by_hash("uuid-alice", "h" * 40) is None

        sql, params = log[0]
        # 窗口是这条 SQL 的核心：去掉它 → "同一个问题两周后又被差评"会被静默吞掉
        assert "NOW() - INTERVAL %s MINUTE" in sql
        assert params == ("uuid-alice", "h" * 40, feedback_service.DEDUPE_WINDOW_MINUTES)

    def test_repeat_within_window_updates_the_existing_row(self, monkeypatch):
        """连点两次 👎 → 只有一条记录（运营看板上的差评数不该因手抖翻倍）。"""
        log, results = [], [{"id": 7, "rating": "up", "created_at": None}]
        monkeypatch.setattr(feedback_service, "get_conn", lambda: _FakeConn(log, results))

        ok = feedback_service.save_feedback(
            "uuid-alice", "both", "张嘉亮是谁？", None, "答案", [], "down", reason="资料缺失"
        )

        assert ok is True
        assert any(sql.startswith("UPDATE feedback SET") for sql, _ in log)
        assert not any(sql.startswith("INSERT INTO feedback") for sql, _ in log)
        update_params = [params for sql, params in log if sql.startswith("UPDATE")][0]
        assert update_params[-1] == 7, "要更新窗口内那条记录的 id"
        assert "created_at" not in log[-1][0], "不改 created_at：那是这条记录第一次出现的时间"

    def test_new_question_inserts_with_the_hash(self, monkeypatch):
        log, results = [], [None]
        monkeypatch.setattr(feedback_service, "get_conn", lambda: _FakeConn(log, results))

        ok = feedback_service.save_feedback(
            "uuid-alice", "public", "SAGE 是什么", "SAGE 是什么", "答案", [], "up"
        )

        assert ok is True
        insert_sql, insert_params = [item for item in log if item[0].startswith("INSERT")][0]
        assert "q_hash" in insert_sql
        assert insert_params[-1] == feedback_service.question_hash("SAGE 是什么")

    def test_database_failure_returns_false_instead_of_raising(self, monkeypatch):
        """``save_feedback`` 的真假值就是路由判 500 的依据，不能改成抛异常。"""

        def _boom():
            raise RuntimeError("mysql down")

        monkeypatch.setattr(feedback_service, "get_conn", _boom)

        assert feedback_service.save_feedback("u", "public", "q", None, "a", [], "up") is False

    def test_summary_is_three_single_table_queries(self, monkeypatch):
        log, results = [], [
            [{"rating": "up", "n": 3}, {"rating": "down", "n": 1}],
            [
                {"scope": "private:uuid-alice", "rating": "up", "n": 1},
                {"scope": "public", "rating": "up", "n": 2},
                {"scope": "public", "rating": "down", "n": 1},
            ],
            [{"reason": "资料缺失", "n": 1}],
        ]
        monkeypatch.setattr(feedback_service, "get_conn", lambda: _FakeConn(log, results))

        summary = feedback_service.summary()

        assert (summary["total"], summary["up"], summary["down"], summary["up_rate"]) == (
            4,
            3,
            1,
            0.75,
        )
        # 样本多的库排前面（并列时按库名）——顺序稳定，界面才不会每次刷新都在跳
        assert summary["by_scope"][0] == {"scope": "public", "up": 2, "down": 1}
        assert summary["top_reasons"] == [{"reason": "资料缺失", "count": 1}]
        assert len(log) == 3
        assert all("JOIN" not in sql.upper() for sql, _ in log), (
            "feedback 与 file_dedup 的排序规则可能不同，统计一律单表聚合"
        )
