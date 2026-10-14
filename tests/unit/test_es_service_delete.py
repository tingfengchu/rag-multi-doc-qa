"""``es_service.delete_by_file_uuid`` 的删除计数（P0-3，纯离线、不连 ES）。

为什么这件事值得单测：
    ``delete_by_query`` 的响应里 ``total`` 是"命中的文档数"、``deleted`` 是
    "真的删掉的文档数"，两者在版本冲突（``conflicts="proceed"`` 时不会中断，
    只会留下没删掉的文档）或分片失败时会不相等。原实现只取 ``deleted`` 就直接
    返回，调用方（文件删除链路 / 重置知识库脚本）会把"删了一半"当成"删干净了"，
    症状是"删掉的文件还能被搜到"，而日志里一切正常。

本文件锁定三件事：
1. 计数取自 ``deleted``（而不是 ``total``，也不是写死 0）；
2. 响应既可能是 ``ObjectApiResponse``（有 ``.body``）也可能是裸 dict / 异常形状，
   都必须能取到计数，取不到时**不能编造**；
3. ``deleted != total`` 或存在 ``failures`` 时必须降级成 warning/error 日志，
   让"没删干净"可被运维发现。
"""
import pytest

from services import es_service


class FakeResponse:
    """``ObjectApiResponse`` 的最小替身：只有 ``.body`` 可用（没有 ``__getitem__``）。"""

    def __init__(self, body):
        self.body = body


@pytest.fixture()
def es_calls(monkeypatch):
    """把 ``es.delete_by_query`` 换成记录调用的替身，返回 (calls, set_response)。"""
    calls = []
    box = {"response": None}

    def fake_delete_by_query(**kwargs):
        calls.append(kwargs)
        return box["response"]

    monkeypatch.setattr(es_service.es, "delete_by_query", fake_delete_by_query)
    return calls, box


def _delete_one(es_calls, body):
    calls, box = es_calls
    box["response"] = body
    return es_service.delete_by_file_uuid("file-1", "public"), calls


class TestCounts:
    def test_returns_deleted_count(self, es_calls):
        deleted, _ = _delete_one(es_calls, FakeResponse({"total": 7, "deleted": 7}))

        assert deleted == 7

    def test_deleted_wins_over_total(self, es_calls):
        """版本冲突场景：命中 7 条但只删掉 5 条 → 必须返回 5（真实删除数）。"""
        deleted, _ = _delete_one(
            es_calls,
            FakeResponse({"total": 7, "deleted": 5, "version_conflicts": 2}),
        )

        assert deleted == 5

    def test_plain_dict_response_is_supported(self, es_calls):
        """老版本 / 假客户端返回裸 dict 时同样要能取到计数。"""
        deleted, _ = _delete_one(es_calls, {"total": 3, "deleted": 3})

        assert deleted == 3

    def test_missing_deleted_is_zero_not_crash(self, es_calls):
        """响应里没有 deleted（异常形状）时返回 0，而不是把 None 交给调用方做加法。"""
        deleted, _ = _delete_one(es_calls, FakeResponse({"total": 2}))

        assert deleted == 0

    def test_unexpected_response_object_does_not_crash(self, es_calls):
        """响应不是 dict 也不是带 body 的对象时，返回值仍必须是 int。"""
        deleted, _ = _delete_one(es_calls, object())

        assert deleted == 0

    def test_query_filters_by_file_and_scope(self, es_calls):
        """删除永远同时按 file_uuid + scope 过滤：漏掉 scope 会删掉别人库里的同名文件。"""
        _, calls = _delete_one(es_calls, FakeResponse({"total": 1, "deleted": 1}))

        filters = calls[0]["query"]["bool"]["filter"]
        assert {"term": {"metadata.file_uuid": "file-1"}} in filters
        assert {"term": {"metadata.scope": "public"}} in filters
        # refresh=True：否则刚删完还能搜到（表现为"删不掉"）
        assert calls[0]["refresh"] is True


class TestLoggingLevels:
    """删干净 / 没删干净 / 分片报错必须是三种不同级别的日志。"""

    def _level(self, monkeypatch, body):
        seen = []

        class Recorder:
            def info(self, event, **kw):
                seen.append(("info", event, kw.get("extra", {})))

            def warning(self, event, **kw):
                seen.append(("warning", event, kw.get("extra", {})))

            def error(self, event, **kw):
                seen.append(("error", event, kw.get("extra", {})))

        monkeypatch.setattr(es_service, "logger", Recorder())
        stats = es_service._delete_by_query_stats(body)
        es_service._log_delete_result("es_deleted_by_file", stats, file_uuid="file-1")
        return seen[0]

    def test_clean_delete_is_info(self, monkeypatch):
        level, event, extra = self._level(monkeypatch, {"total": 4, "deleted": 4})

        assert level == "info"
        assert event == "es_deleted_by_file"
        assert extra["deleted"] == 4 and extra["file_uuid"] == "file-1"

    def test_partial_delete_is_warning(self, monkeypatch):
        level, event, extra = self._level(
            monkeypatch, {"total": 4, "deleted": 3, "version_conflicts": 1}
        )

        assert level == "warning"
        assert event.endswith("_incomplete")
        assert extra["conflicts"] == 1

    def test_failure_is_error(self, monkeypatch):
        level, event, _ = self._level(
            monkeypatch, {"total": 4, "deleted": 0, "failures": [{"reason": "boom"}]}
        )

        assert level == "error"
        assert event.endswith("_failed")


class TestDeleteByScope:
    def test_scope_delete_uses_same_parsing(self, monkeypatch):
        box = {"body": {"total": 2, "deleted": 2}}
        monkeypatch.setattr(
            es_service.es, "delete_by_query", lambda **kw: FakeResponse(box["body"])
        )

        assert es_service.delete_by_scope("public") == 2
