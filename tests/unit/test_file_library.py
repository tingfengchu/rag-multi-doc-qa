"""``GET /api/files/library``：文件管理面板的数据（端点级、离线）。

面板要回答的问题与 ``GET /files?scope=`` 不同：不是"某个库里有什么"，而是
"**我现在能看到哪些原件**"。所以这里要钉死的是：

* 查的库恒为 ``[public, private:<会话里的 uuid>]`` —— 别人的私人库不是"前端不显示"，
  而是后端**根本没查**（请求里带任何 scope 参数都影响不了它）；
* ``version``（同名文件的第几个）按 ``(库, 文件名)`` 递增，且同一份数据每次刷新都一样；
* ``downloadable`` / ``size_bytes`` 如实反映"原件在不在磁盘上"（改造前上传的文件是 False）。

离线策略：替身 ``file_state``（不连 MySQL）+ 真 ``file_store``（写到 tmp_path）。
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.deps import get_current_user
from api.main import app
from api.routers import files as files_router
from config import settings
from services import file_store
from utils.roles import ROLE_ADMIN, ROLE_AUTHORIZED_USER, ROLE_USER

ALICE = {"user_uuid": "uuid-alice", "username": "alice", "role": ROLE_USER}
ROOT = {"user_uuid": "uuid-root", "username": "root", "role": ROLE_ADMIN}
#: 被授权的普通用户（模块 3）：能写公共库，但没有管理端权限。
EDITOR = {"user_uuid": "uuid-editor", "username": "editor", "role": ROLE_AUTHORIZED_USER}

WHEN = datetime(2026, 10, 13, 18, 5, 7)


def _row(file_uuid, file_name, scope, owner_uuid="uuid-root", owner_username="root",
         updated_at=WHEN, created_at=WHEN, version=1, is_archived=0):
    """一行 ``file_state.list_library_files`` 的返回（字段与 SQL 的 SELECT 对齐）。"""
    return {
        "file_uuid": file_uuid,
        "file_name": file_name,
        "scope": scope,
        "owner_uuid": owner_uuid,
        "source": f"{owner_uuid}__{file_uuid}__{file_name}",
        # 模块 5：这两列现在来自 file_dedup（migration 004），不再由列表层算
        "version": version,
        "is_archived": is_archived,
        "created_at": created_at,
        "updated_at": updated_at,
        "owner_username": owner_username,
    }


class FakeFileState:
    """只记录"问了哪些库"，并按这些库返回预置的行（含 ``is_archived`` 过滤）。"""

    def __init__(self, rows_by_scope):
        self.rows_by_scope = rows_by_scope
        self.asked = []
        #: 每次调用收到的 ``include_archived``（面板开关的传参要能被断言）
        self.asked_archived = []

    def list_library_files(self, scopes, include_archived=False):
        self.asked.append(list(scopes))
        self.asked_archived.append(include_archived)
        rows = [row for scope in scopes for row in self.rows_by_scope.get(scope, [])]
        if include_archived:
            return rows
        # 真实 SQL 用 ``AND is_archived = 0`` 过滤，替身照做——否则"默认只列最新版本"
        # 这条断言在离线测试里根本验不到（替身会把历史版本也吐出来）。
        return [row for row in rows if not row.get("is_archived")]


PUBLIC_ROWS = [
    _row("u-1", "自动驾驶算法与嵌入式具身智能运控.pdf", "public"),
    _row("u-2", "1790569531500.pdf", "public"),
    _row("u-3", "SAGE AI 智能问答助手.pdf", "public"),
]


@pytest.fixture()
def fake_state(monkeypatch, tmp_path):
    rows = {"public": list(PUBLIC_ROWS), "private:uuid-alice": []}
    fake = FakeFileState(rows)
    monkeypatch.setattr(files_router, "file_state", fake)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(settings, "file_store_dir", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "file_legacy_search_dirs", str(legacy))
    # 只有 1 号文件有留存件，另外两个走兜底目录（模拟交付环境：原件放在项目根目录）
    file_store.save_original("u-1", "自动驾驶算法与嵌入式具身智能运控.pdf", b"a" * 7)
    (legacy / "1790569531500.pdf").write_bytes(b"b" * 11)
    return fake


@pytest.fixture()
def client(fake_state):
    c = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: ALICE
    yield c
    app.dependency_overrides.clear()


def _library(client, **params):
    resp = client.get("/api/files/library", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestScopeIsServerSide:
    """面板查哪些库，由服务端说了算。"""

    def test_asks_for_public_plus_session_private_only(self, client, fake_state):
        _library(client)

        assert fake_state.asked == [["public", "private:uuid-alice"]]

    def test_client_scope_param_is_ignored(self, client, fake_state):
        """带 ``scope=private:<别人的 uuid>`` 也影响不了它——**不是**靠前端不传。"""
        _library(client, scope="private:uuid-bob")

        assert fake_state.asked == [["public", "private:uuid-alice"]]

    def test_admin_sees_the_same_two_scopes(self, client, fake_state):
        """管理员**不**额外获得"别人的私人库"：与 ``deps.resolve_scope`` 的能力边界一致。"""
        app.dependency_overrides[get_current_user] = lambda: ROOT

        _library(client)

        assert fake_state.asked == [["public", "private:uuid-root"]]

    def test_requires_login(self, fake_state):
        """没有会话 → 401。刻意**不加** dependency override（也就不会触发 lifespan）。"""
        c = TestClient(app)

        assert c.get("/api/files/library").status_code == 401


class TestRowShape:
    """面板要用到的每个字段都要真的算出来（前端不再自己拼）。"""

    def test_scope_label_and_owner_are_filled(self, client):
        first = _library(client)[0]

        assert first["scope"] == "public"
        assert first["scope_label"] == "公共库", "中文库名由后端给，前端不重写一份映射"
        assert first["owner_username"] == "root"
        # source 是内部唯一键，不该出现在给用户看的列表里
        assert "source" not in first

    def test_private_rows_are_labelled_as_mine(self, client, fake_state):
        fake_state.rows_by_scope["private:uuid-alice"] = [
            _row("u-9", "我的简历 (AI应用开发)(3).pdf", "private:uuid-alice",
                 owner_uuid="uuid-alice", owner_username="alice")
        ]

        row = _library(client)[-1]

        assert row["scope_label"] == "我的文档"
        assert row["owner_username"] == "alice"

    def test_updated_at_is_serialized(self, client):
        assert _library(client)[0]["updated_at"].startswith("2026-10-13T18:05:07")

    def test_falls_back_to_created_at_when_updated_at_is_missing(self, client, fake_state):
        """老数据 ``updated_at`` 为空 → 退回 ``created_at``，不显示"—"（时间总是有的）。"""
        fake_state.rows_by_scope["public"] = [_row("u-1", "a.pdf", "public", updated_at=None)]

        assert _library(client)[0]["updated_at"].startswith("2026-10-13T18:05:07")

    def test_missing_owner_account_shows_dash(self, client, fake_state):
        """上传人账号被清理（LEFT JOIN 回空串）→ 面板显示"—"，但文件仍然列出来。"""
        fake_state.rows_by_scope["public"] = [
            _row("u-1", "a.pdf", "public", owner_uuid="uuid-gone", owner_username="")
        ]

        assert _library(client)[0]["owner_username"] == ""


class TestDownloadability:
    """``downloadable`` / ``size_bytes``：原件在不在磁盘上，如实告诉前端。"""

    def test_stored_original_is_downloadable_with_size(self, client):
        first = _library(client)[0]

        assert first["downloadable"] is True
        assert first["size_bytes"] == 7

    def test_legacy_original_counts_as_downloadable(self, client):
        """兜底目录里的原件同样可下载（3 个公共库文档靠它）。"""
        second = _library(client)[1]

        assert second["downloadable"] is True
        assert second["size_bytes"] == 11

    def test_record_without_any_original_is_marked_not_downloadable(self, client):
        """前端据此把按钮置灰并说明原因，而不是让用户点了才吃 404。"""
        third = _library(client)[2]

        assert third["downloadable"] is False
        assert third["size_bytes"] is None


class TestVersionsAndArchive:
    """``version`` / ``archived``（模块 5）：**库里的真实列**，不再由列表层"数出来"。

    为什么这组断言重要：改造前版本号是"列表按顺序累加"，所以它只能验证"排序恰好正确时
    算得对"；一旦排序方式变了（模块 5 把同名版本聚在一起、新版在前），累加出来的号码
    会当场乱掉——而乱掉的版本号在界面上几乎看不出来（都是 v1）。
    """

    def test_version_comes_from_the_database_column(self, client, fake_state):
        """库里记着 v2/v1，界面就显示 v2/v1：与返回顺序无关。"""
        fake_state.rows_by_scope["public"] = [
            _row("u-2", "手册.pdf", "public", version=2),
            _row("u-1", "手册.pdf", "public", version=1),
            _row("u-3", "另一个.pdf", "public", version=1),
        ]

        assert [(row["file_name"], row["version"]) for row in _library(client)] == [
            ("手册.pdf", 2),
            ("手册.pdf", 1),
            ("另一个.pdf", 1),
        ]

    def test_archived_rows_are_hidden_by_default(self, client, fake_state):
        """默认只列最新版本：历史版本不该出现在"我现在有哪些原件"里。"""
        fake_state.rows_by_scope["public"] = [
            _row("u-2", "手册.pdf", "public", version=2),
            _row("u-1", "手册.pdf", "public", version=1, is_archived=1),
        ]

        assert [row["file_uuid"] for row in _library(client)] == ["u-2"]

    def test_include_archived_lists_history_with_the_marker(self, client, fake_state):
        """打开「显示历史版本」→ 两行都在，且历史版本带 ``archived=True``（前端据此标灰）。"""
        fake_state.rows_by_scope["public"] = [
            _row("u-2", "手册.pdf", "public", version=2),
            _row("u-1", "手册.pdf", "public", version=1, is_archived=1),
        ]

        rows = _library(client, include_archived="true")

        assert [row["file_uuid"] for row in rows] == ["u-2", "u-1"]
        assert [row["archived"] for row in rows] == [False, True]
        # 两个版本的文件名/库完全相同：区分它们只能靠版本号与归档标记
        assert {row["file_name"] for row in rows} == {"手册.pdf"}

    def test_flag_is_off_unless_the_client_asks(self, client, fake_state):
        """默认 ``include_archived=False``：这是"面板平时不显示历史版本"的落点。"""
        _library(client)
        _library(client, include_archived="false")
        _library(client, include_archived="true")

        assert fake_state.asked_archived == [False, False, True]

    def test_same_name_in_two_scopes_is_numbered_per_scope(self, client, fake_state):
        """版本号按 ``(库, 文件名)`` 分组：私人库里的"手册.pdf"不接着公共库往下数。"""
        fake_state.rows_by_scope["public"] = [_row("u-1", "手册.pdf", "public")]
        fake_state.rows_by_scope["private:uuid-alice"] = [
            _row("u-2", "手册.pdf", "private:uuid-alice", owner_uuid="uuid-alice")
        ]

        assert [row["version"] for row in _library(client)] == [1, 1]

    def test_old_database_without_the_new_columns_still_renders(self, client, fake_state):
        """老库（还没跑 migration 004）没有这两列 → 按 v1 + 非归档显示，**不报错**。

        交付现场是"先部署、再补列"的顺序，中间那一刻 fail 成 500 会让面板整页空白——
        宁可显示得保守一点，也不能把已经存在的文件列表搞没。
        """
        row = _row("u-1", "手册.pdf", "public")
        row.pop("version")
        row.pop("is_archived")
        fake_state.rows_by_scope["public"] = [row]

        item = _library(client)[0]

        assert item["version"] == 1
        assert item["archived"] is False


class _FakeCursor:
    """只记录 SQL、按顺序吐出预置结果（不连 MySQL）。"""

    def __init__(self, log, results):
        self.log, self.results = log, results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.log.append((sql, tuple(params)))

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


FILE_ROW = {
    "file_uuid": "u-1",
    "file_name": "a.pdf",
    "scope": "public",
    "owner_uuid": "o-1",
    "source": "o-1__u-1__a.pdf",
    "created_at": WHEN,
    "updated_at": WHEN,
}


class TestOwnerNameMerge:
    """上传人用户名是**第二条查询**取回来的（``services/file_state._usernames_for``）。

    为什么单独立一组：这里踩过一次真实故障（2026-10-13）——
    原本写成 ``LEFT JOIN users``，而 ``file_dedup``（运行期建的，MySQL 8 默认
    ``utf8mb4_0900_ai_ci``）与 ``users``（migration 001 的 ``utf8mb4_unicode_ci``）
    **排序规则不同**，一 JOIN 就 500::

        1267 (HY000): Illegal mix of collations ... for operation '='

    用户看到的是"文件管理面板里一个文件都没有"。所以这条断言（SQL 里不许出现 JOIN）
    不是洁癖，它守着一个已经发生过的事故。
    """

    def test_owner_name_is_merged_from_a_separate_query(self, monkeypatch):
        from services import file_state

        log, results = [], [[dict(FILE_ROW)], [{"user_uuid": "o-1", "username": "root"}]]
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        rows = file_state.list_library_files(["public"])

        assert rows[0]["owner_username"] == "root"
        assert len(log) == 2, "两列全表都点名要的用户名，应该只有两条查询（不是 N+1）"

    def test_no_cross_table_join(self, monkeypatch):
        from services import file_state

        log, results = [], [[dict(FILE_ROW)], [{"user_uuid": "o-1", "username": "root"}]]
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        file_state.list_library_files(["public"])

        assert all("JOIN" not in sql.upper() for sql, _ in log), (
            "file_dedup 与 users 的排序规则不同，JOIN 会报 1267 → 必须分开查再合并"
        )

    def test_row_survives_when_the_owner_account_is_gone(self, monkeypatch):
        """上传人账号被清理（第二条查询查不到）→ 文件仍要列出来，用户名回空串。"""
        from services import file_state

        log, results = [], [[dict(FILE_ROW)], []]
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        rows = file_state.list_library_files(["public"])

        assert [row["file_uuid"] for row in rows] == ["u-1"]
        assert rows[0]["owner_username"] == ""

    def test_empty_scope_list_short_circuits(self, monkeypatch):
        from services import file_state

        monkeypatch.setattr(
            file_state, "get_conn", lambda: pytest.fail("空 scope 不该去连数据库")
        )

        assert file_state.list_library_files([]) == []


class TestRouteShape:
    def test_library_path_is_not_parsed_as_a_file_uuid(self, client):
        """"/library" 必须命中列表端点（若被 ``/{file_uuid}/download`` 之类吃掉，会变成 422）。"""
        assert client.get("/api/files/library").status_code == 200

    def test_does_not_change_the_single_scope_listing(self, client, monkeypatch):
        """老端点语义不动：``GET /files?scope=public`` 仍然只查那一个库。"""
        calls = []
        monkeypatch.setattr(
            files_router,
            "file_state",
            type("S", (), {
                "list_success_files": lambda self, scope, include_archived=False: (
                    calls.append((scope, include_archived)) or []
                )
            })(),
        )

        assert client.get("/api/files", params={"scope": "public"}).json() == []
        assert client.get(
            "/api/files", params={"scope": "public", "include_archived": "true"}
        ).json() == []
        # 模块 5：默认只列最新版本；客户端要历史版本时如实转发（不是前端自己过滤）
        assert calls == [("public", False), ("public", True)]


class TestCanDelete:
    """「删除」按钮的可见性（2026-10-13 验收）：**由后端算**，且与删除接口同一份规则。

    前端只读 ``can_delete``（不自己判角色），所以这里钉死的其实是"这个布尔值与
    ``DELETE /files/{uuid}`` 的结论一致"，而不是"某个函数碰巧返回了 True"。
    """

    def test_normal_user_cannot_delete_public_rows(self, client):
        """普通用户对公共库的行一律没有删除入口（写公共库是 403，见 test_scope_auth）。"""
        assert [row["can_delete"] for row in _library(client)] == [False, False, False]

    def test_admin_can_delete_public_rows(self, client):
        app.dependency_overrides[get_current_user] = lambda: ROOT

        assert all(row["can_delete"] for row in _library(client))

    def test_authorized_user_can_delete_public_rows(self, client):
        """``authorized_user``（模块 3）的"写公共库"包含删除：两处必须是同一个角色集合。"""
        app.dependency_overrides[get_current_user] = lambda: EDITOR

        assert all(row["can_delete"] for row in _library(client))

    def test_own_private_row_is_deletable_by_a_normal_user(self, client, fake_state):
        """私人库只有本人看得见，所以"自己的私人库"对普通用户永远是能删的。"""
        fake_state.rows_by_scope["private:uuid-alice"] = [
            _row(
                "u-9",
                "我的笔记.md",
                "private:uuid-alice",
                owner_uuid="uuid-alice",
                owner_username="alice",
            )
        ]

        assert _library(client)[-1]["can_delete"] is True

    def test_other_peoples_private_row_is_never_deletable(self, client, fake_state):
        """脏数据兜底：行里的 scope 若是**别人的**私人库，一律不给删除按钮（fail-closed）。

        正常路径下列表根本查不到别人的私人库（见 ``TestScopeIsServerSide``）。这条防的是
        "将来某次改动把查询范围放宽之后，删除按钮跟着一起放出去"——那才是真正的越权。
        """
        fake_state.rows_by_scope["public"] = [
            _row("u-9", "别人的笔记.md", "private:uuid-bob", owner_uuid="uuid-bob")
        ]

        assert _library(client)[0]["can_delete"] is False

    def test_unknown_scope_is_not_deletable(self, client, fake_state):
        """scope 认不出来时按"不能删"处理，而不是"默认放开"。"""
        fake_state.rows_by_scope["public"] = [_row("u-9", "怪文件.md", "weird:scope")]

        assert _library(client)[0]["can_delete"] is False

    def test_archived_row_is_deletable_too(self, client, fake_state):
        """历史版本不是另一类数据：能删最新版就该能删它的旧版（否则又成了删不掉的幽灵）。"""
        app.dependency_overrides[get_current_user] = lambda: ROOT
        fake_state.rows_by_scope["public"] = [
            _row("u-1", "手册.pdf", "public", version=2),
            _row("u-2", "手册.pdf", "public", version=1, is_archived=1),
        ]

        archived = _library(client, include_archived="true")[-1]

        assert archived["archived"] is True
        assert archived["can_delete"] is True

    def test_button_visibility_matches_what_the_delete_endpoint_allows(self, client):
        """同一个文件：列表说"不给按钮" ⟺ 删除接口回 403（不是 200，也不是 500）。"""
        target = _library(client)[0]

        assert target["can_delete"] is False
        denied = client.delete(f"/api/files/{target['file_uuid']}", params={"scope": "public"})

        assert denied.status_code == 403
        assert "管理员" in denied.json()["detail"]
