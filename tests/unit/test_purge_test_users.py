"""测试账号保洁（``scripts/purge_e2e_users``）的**离线**单测。

为什么必须离线：这段代码删的是**用户表**——跑测试的机器未必有 MySQL / ``.env``。
这里把 ``db`` 模块整个换成假的（预置进 ``sys.modules``），于是既不连库、也不 import
``config``，却仍然钉住了真正危险的部分：**删除条件**（只删白名单前缀）与**返回值**
（实际删掉的条数）。
"""
import importlib
import sys
import types

import pytest

PREFIXES = ("e2e_", "test_")


class _FakeCursor:
    def __init__(self, log, rowcount):
        self._log = log
        self._rowcount = rowcount
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._log.append((sql, params))
        self.rowcount = self._rowcount

    def fetchone(self):
        return (7,)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, log, rowcount):
        self._log = log
        self._rowcount = rowcount

    def cursor(self):
        return _FakeCursor(self._log, self._rowcount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def purge(monkeypatch):
    """返回 ``(模块, 执行的 SQL 日志)``；``db.get_conn`` 换成假的。"""
    log = []
    fake_db = types.ModuleType("db")
    fake_db.get_conn = lambda: _FakeConn(log, rowcount=3)
    monkeypatch.setitem(sys.modules, "db", fake_db)

    module = importlib.import_module("scripts.purge_e2e_users")
    return importlib.reload(module), log


def test_only_whitelisted_prefixes_are_deleted(purge):
    """只删白名单前缀——绝不按"最近创建"删（那迟早删到真人账号）。"""
    module, log = purge

    module.purge_test_users()

    assert [params[0] for _, params in log] == [f"{prefix}%" for prefix in PREFIXES]
    for sql, _ in log:
        assert sql.startswith("DELETE FROM users WHERE username LIKE ")


def test_prefix_is_bound_not_interpolated(purge):
    """前缀走参数绑定：拼接 SQL 等于把注入留给下一个改这里的人。"""
    module, log = purge

    module.purge_test_users()

    for sql, params in log:
        assert "%s" in sql
        assert params is not None and params[0].endswith("%")


def test_return_value_is_the_number_of_deleted_rows(purge):
    """返回值是"实际删了几条"（用来打印/断言），不是"执行了几条 SQL"。"""
    module, _ = purge

    assert module.purge_test_users() == 6   # 2 个前缀 × 每个 3 行


def test_conftest_fixture_swallows_database_errors(monkeypatch, capsys):
    """连不上库时静默跳过：纯离线跑单测的机器不该因为保洁夹具变红。"""
    from tests import conftest

    def boom():
        raise RuntimeError("这里没有 MySQL")

    fake = types.ModuleType("scripts.purge_e2e_users")
    fake.purge_test_users = boom
    monkeypatch.setitem(sys.modules, "scripts.purge_e2e_users", fake)

    fixture = getattr(conftest._purge_test_accounts, "__wrapped__", conftest._purge_test_accounts)
    generator = fixture()
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)

    assert "已跳过" in capsys.readouterr().out
