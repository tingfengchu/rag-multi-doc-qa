"""模块 5：文件版本管理（同名上传 → 旧版归档、新版本 +1）。

为什么这组测试要用**状态化**替身（而不是 ``test_upload_scope.py`` 那种只记录调用的）：
验收点本身就是状态——"同名两次上传 → 旧记录 ``is_archived=1``、新记录 ``version=2``"。
只有让替身真的维护 rows，才能把整条链路（算版本 → 建 pending → 入库成功 → 归档旧版）
跑一遍；只记录调用的话，"先归档再入库"这种顺序错误照样能全绿通过。

覆盖的边界（每条都对应一个真实的坏结果）：

* 同名不同内容 → **允许**（改造前被 MD5 查重拦下，用户以为"更新文档"失败了）；
* 同名不同内容 → **允许**，version + 1 且旧版归档（改造前被 MD5 查重拦下，用户以为"更新文档"失败了）；
* 同名**相同内容** → **去重**，不新建版本（2026-10-13 验收修正：同一份文件传两遍曾被记成 v2 并把 v1 归档，
  面板上于是并排出现两条内容一模一样的记录，而用户其实只是想确认文件在不在）；
* 不同名相同内容 → 仍然拒收（那是真的重复，存两份只会多一套 ES 块/向量）；
* 同一份内容传进**另一个库** → 正常入库（去重按 (库, MD5) 判定，不是全局判重）；
* 不同名相同内容 → 仍然拒收（那是真的重复，存两份只会多一套 ES 块/向量）；
* 入库失败 → **不归档**旧版本（否则用户会看到"刚传的没了，旧的也没了"）；
* 删除历史版本 → 能删（``list_success_files`` 必须带上 ``include_archived``）。
"""
import types

import pytest
from fastapi.testclient import TestClient

from api.deps import get_current_user
from api.main import app
from api.routers import files as files_router
from utils.roles import ROLE_ADMIN

ADMIN = {"user_uuid": "uuid-root", "username": "root", "role": ROLE_ADMIN}


class MemoryFileState:
    """``services.file_state`` 的内存实现（只实现上传/删除路径用到的那几个函数）。"""

    def __init__(self):
        self.rows = {}
        self.archived_calls = []
        #: (scope, file_hash)：模拟"内容已入库"（内容级查重用的那份事实）
        self.content_seen = set()
        self.deleted = []
        #: (scope, include_archived)：删除路径**必须**带 True 才找得到历史版本
        self.list_calls = []

    # ---------------- 读 ----------------
    def latest_version(self, scope, file_name):
        candidates = [
            row for row in self.rows.values()
            if row["scope"] == scope
            and row["file_name"] == file_name
            and row["status"] == "success"
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda row: row["version"])
        return {"file_uuid": best["file_uuid"], "version": best["version"]}

    def check_duplicate(self, file_hash, scope):
        return {"source": "existing"} if (scope, file_hash) in self.content_seen else None

    def list_success_files(self, scope, include_archived=False):
        self.list_calls.append((scope, include_archived))
        return [
            dict(row)
            for row in self.rows.values()
            if row["scope"] == scope
            and row["status"] == "success"
            and (include_archived or not row["is_archived"])
        ]

    # ---------------- 写 ----------------
    def create_pending(self, file_hash, scope, file_name, source, owner_uuid, file_uuid,
                       version=None):
        assert version is not None, "REST 上传路径必须显式传版本号（见 api/routers/files.py）"
        self.rows[file_uuid] = {
            "file_uuid": file_uuid,
            "file_name": file_name,
            "scope": scope,
            "source": source,
            "owner_uuid": owner_uuid,
            "file_hash": file_hash,
            "status": "pending",
            "version": version,
            "is_archived": 0,
        }
        return 1

    def mark_success(self, file_uuid, scope):
        self.rows[file_uuid]["status"] = "success"
        self.content_seen.add((scope, self.rows[file_uuid]["file_hash"]))

    def mark_failed(self, file_uuid, scope):
        self.rows[file_uuid]["status"] = "failed"

    def archive_versions(self, scope, file_name, keep_file_uuid):
        self.archived_calls.append((scope, file_name, keep_file_uuid))
        archived = 0
        for row in self.rows.values():
            if (
                row["scope"] == scope
                and row["file_name"] == file_name
                and row["status"] == "success"
                and row["file_uuid"] != keep_file_uuid
            ):
                row["is_archived"] = 1
                archived += 1
        return archived

    def delete_record(self, file_uuid, scope):
        self.deleted.append((file_uuid, scope))
        if self.rows.get(file_uuid, {}).get("scope") == scope:
            self.rows.pop(file_uuid)


class FakePdfService:
    """不跑 Docling：直接给出"父块 1 / 子块 1"的最小合法结构。"""

    def process_uploaded_document_with_parent_child(self, uploaded, user_uuid, file_uuid, source):
        meta = {"parent_id": "p1", "page": 1, "Header 1": "标题"}
        return {
            "parents": [types.SimpleNamespace(page_content="父块", metadata=dict(meta))],
            "children": [types.SimpleNamespace(page_content="子块", metadata=dict(meta))],
        }



class FakeVectorService:
    def get_vectorstore(self, scope):
        return f"chroma:{scope}"

    def add_documents(self, vectorstore, children):
        return None

    def delete_by_source(self, vectorstore, source):
        return None


class FakeEsService:
    def index_parent_chunk(self, *args, **kwargs):
        return None

    def index_child_chunk(self, *args, **kwargs):
        return None

    def delete_by_file_uuid(self, *args, **kwargs):
        return None

    def refresh_index(self):
        return None


class FakeFileStore:
    def __init__(self):
        self.saved, self.removed = [], []

    def save_original(self, file_uuid, filename, content):
        self.saved.append((file_uuid, filename, content))
        return f"memory://{file_uuid}"

    def remove_original(self, file_uuid, filename):
        self.removed.append((file_uuid, filename))
        return True


@pytest.fixture()
def state(monkeypatch):
    """模块 5 上传链路所需的全部替身（真路由 + 真依赖注入 + 真权限判断）。"""
    fake = MemoryFileState()
    monkeypatch.setattr(files_router, "file_state", fake)
    monkeypatch.setattr(files_router, "pdf_service", FakePdfService())
    monkeypatch.setattr(files_router, "vector_service", FakeVectorService())
    monkeypatch.setattr(files_router, "es_service", FakeEsService())
    monkeypatch.setattr(files_router, "file_store", FakeFileStore())
    return fake


@pytest.fixture()
def client(state):
    c = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield c
    app.dependency_overrides.clear()


def _upload(client, filename="demo.md", body=b"# v1\n"):
    return client.post(
        "/api/files/upload",
        data={"scope": "public"},
        files={"file": (filename, body, "text/markdown")},
    )


def _rows(state, file_name="demo.md"):
    return sorted(
        [row for row in state.rows.values() if row["file_name"] == file_name],
        key=lambda row: row["version"],
    )


class TestSameNameUpload:
    """同名上传 = 新版本（模块 5 的核心行为）。"""

    def test_first_upload_is_version_1_and_not_archived(self, client, state):
        resp = _upload(client)

        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == 1
        row = _rows(state)[0]
        assert (row["version"], row["is_archived"]) == (1, 0)

    def test_second_upload_same_name_archives_the_old_one(self, client, state):
        """同名两次上传 → 旧版 archived=1、新版 version=2（验收清单原句）。"""
        first = _upload(client, body=b"# v1\n")
        assert first.json()["version"] == 1

        second = _upload(client, body=b"# v2\n")
        assert second.status_code == 200, second.text
        assert second.json()["version"] == 2
        assert second.json()["duplicate"] is False, "同名但内容不同 = 新版本，不是重复"

        rows = _rows(state)
        assert [(row["version"], row["is_archived"]) for row in rows] == [(1, 1), (2, 0)]
        assert rows[0]["file_uuid"] != rows[1]["file_uuid"], "新版本是独立记录，不是原地覆盖"
        # 每次成功上传都会调一次归档（第一次没有同名旧版，归档 0 行）；关键是**最后一次**
        # 必须带上刚入库成功的那条 uuid，否则会把新版本自己归档掉。
        assert state.archived_calls[-1] == ("public", "demo.md", second.json()["file_uuid"])
        assert state.archived_calls[-1][2] != first.json()["file_uuid"]

    def test_identical_content_with_the_same_name_is_deduplicated(self, client, state):
        """同名 + 内容一模一样（MD5 相同）→ **去重**：还是那一条 v1，既没有 v2、也没有历史版本。

        2026-10-13 验收修正（这里原来断言的是"记成 v2"）。判据必须落在**内容**上：
        用户把同一份文件又传一遍，不是"换了一版"，只是没看到上一次成功的提示——
        记成 v2 会同时毁掉两件事：面板上出现两条内容一模一样的记录（v1 还被标成「历史版本」），
        以及"版本"这个信息本身的意义。
        """
        first = _upload(client, body=b"same bytes\n")
        assert first.json()["version"] == 1

        again = _upload(client, body=b"same bytes\n")

        assert again.status_code == 200, again.text
        assert again.json()["duplicate"] is True
        assert "已入库" in again.json()["message"]
        assert again.json()["file_uuid"] is None, "去重不该产生新记录"
        assert again.json()["version"] is None, "去重不该给出新版本号"

        assert [(row["version"], row["is_archived"]) for row in _rows(state)] == [
            (1, 0)
        ], "库里只该有那一条 v1：没有 v2，也没有被归档的旧版"
        # 归档只在**第一次**成功上传时被调用过一次（那次没有同名旧版，归档 0 行）；
        # 重复上传一步都不该走到归档——否则 v1 会莫名变成「历史版本」。
        assert len(state.archived_calls) == 1
        assert len(files_router.file_store.saved) == 1, "重复上传不该再留一份原件"

    def test_the_same_content_in_another_scope_is_not_a_duplicate(self, client, state):
        """去重按 (库, MD5) 判定：同一份文件传进**另一个库**是正常入库，不该被拦。"""
        _upload(client, filename="public.md", body=b"shared bytes\n")

        other = client.post(
            "/api/files/upload",
            data={"scope": "private:uuid-root"},
            files={"file": ("mine.md", b"shared bytes\n", "text/markdown")},
        )

        assert other.status_code == 200, other.text
        assert other.json()["duplicate"] is False, "另一个库里没有这份内容，不该判重复"
        assert other.json()["version"] == 1
        assert len(state.rows) == 2, "两个库各一份，互不影响"

    def test_renamed_identical_file_is_still_rejected(self, client, state):
        """**不同名**但内容相同 → 仍然拒收：那份内容已经在库里，再存一份只是多一套块。"""
        _upload(client, filename="a.md", body=b"same bytes\n")

        renamed = _upload(client, filename="b.md", body=b"same bytes\n")

        assert renamed.json()["duplicate"] is True
        assert "已入库" in renamed.json()["message"]

    def test_third_upload_continues_the_series(self, client, state):
        """版本号是"同名最新 + 1"，不是"记录条数"：删掉中间那版也不会重复编号。"""
        _upload(client, body=b"# v1\n")
        _upload(client, body=b"# v2\n")
        state.delete_record(_rows(state)[-1]["file_uuid"], "public")

        third = _upload(client, body=b"# v3\n")

        assert third.json()["version"] == 2, "删掉 v2 后，最新还是 v1 → 下一个就是 v2"

    def test_two_scopes_are_numbered_independently(self, client, state):
        """版本号按 (库, 文件名) 算：公共库的 demo.md 与私人库的互不影响。"""
        _upload(client)
        resp = client.post(
            "/api/files/upload",
            data={"scope": "private:uuid-root"},
            files={"file": ("demo.md", b"# private\n", "text/markdown")},
        )

        assert resp.json()["version"] == 1
        assert sorted(row["version"] for row in state.rows.values()) == [1, 1]

    def test_failed_upload_does_not_archive_the_old_version(self, client, state, monkeypatch):
        """入库失败 → 旧版本**保持原样**（不能被归档）。

        这是"归档必须放在 mark_success 之后"的落点：先归档再入库，一旦中间失败，
        用户会看到"刚传的没了，旧的也变成历史版本了"，而那份旧文件其实还在库里。
        """
        _upload(client, body=b"# v1\n")

        def _boom(*args, **kwargs):
            raise ValueError("Docling 未能从该文档中提取到任何有效文本。")

        monkeypatch.setattr(
            files_router,
            "pdf_service",
            types.SimpleNamespace(process_uploaded_document_with_parent_child=_boom),
        )
        resp = _upload(client, body=b"# v2 broken\n")

        assert resp.status_code == 400
        rows = _rows(state)
        assert [(row["version"], row["status"], row["is_archived"]) for row in rows] == [
            (1, "success", 0),
            (2, "failed", 0),
        ]
        # 只有**第一次**（成功的）上传调过归档：失败那次一步都不该走到归档
        assert len(state.archived_calls) == 1


class TestArchivedVersionLifecycle:
    """历史版本"看得见、下得动、删得掉"——三者缺一就是幽灵数据。"""

    def test_archived_version_can_still_be_deleted(self, client, state):
        """删除路径必须 ``include_archived=True``，否则归档版本删不掉。

        不带它时，后端在"成功文件"列表里找不到那条归档记录 → 回
        404「文件不存在或已被删除」，可它刚刚还在面板里显示着，
        用户只能去数据库手工清。
        """
        _upload(client, body=b"# v1\n")
        v1 = _rows(state)[0]["file_uuid"]
        _upload(client, body=b"# v2\n")
        assert _rows(state)[0]["is_archived"] == 1

        resp = client.delete(f"/api/files/{v1}", params={"scope": "public"})

        assert resp.status_code == 200, resp.text
        assert [row["version"] for row in _rows(state)] == [2]
        assert ("public", True) in state.list_calls


class _FakeCursor:
    """只记录 SQL、按顺序吐出预置结果（不连 MySQL）。"""

    def __init__(self, log, results):
        self.log, self.results = log, results
        self.rowcount = 1
        #: ``create_pending`` 会读它当返回值
        self.lastrowid = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        # 空白折叠后再比对：SQL 是分几行拼出来的，断言时不该被换行影响
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


class TestVersionStateSql:
    """离线测试里没有 MySQL，SQL 就是这套逻辑唯一的"事实来源"，形状必须钉住。"""

    def test_latest_version_only_counts_success_rows_of_that_name(self, monkeypatch):
        from services import file_state

        log, results = [], [{"file_uuid": "u-2", "version": 2}]
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        assert file_state.latest_version("public", "手册.pdf") == {
            "file_uuid": "u-2",
            "version": 2,
        }

        sql, params = log[0]
        # 失败/进行中的记录不能参与编号，否则一次失败的上传会"吃掉"一个版本号
        assert "status = 'success'" in sql
        assert "ORDER BY version DESC" in sql
        assert params == ("public", "手册.pdf")

    def test_archive_versions_skips_the_new_version_and_non_success_rows(self, monkeypatch):
        from services import file_state

        log, results = [], []
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        file_state.archive_versions("public", "手册.pdf", keep_file_uuid="u-2")

        sql, params = log[0]
        assert sql.startswith("UPDATE file_dedup SET is_archived = 1")
        assert "status = 'success'" in sql
        # 用 file_uuid 排除新版本（而不是 version < N）：不会把新版本自己归档掉
        assert "file_uuid <> %s" in sql
        assert params == ("public", "手册.pdf", "u-2")

    def test_default_listing_filters_archived_rows_in_sql(self, monkeypatch):
        from services import file_state

        log, results = [], [[], []]
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        file_state.list_success_files("public")
        file_state.list_success_files("public", include_archived=True)

        assert "is_archived = 0" in log[0][0], "默认必须只列最新版本（列表下拉依赖它）"
        assert "is_archived = 0" not in log[1][0], "要历史版本时不能再过滤"

    def test_library_listing_groups_versions_of_the_same_name(self, monkeypatch):
        from services import file_state

        log, results = [], [[], []]
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        file_state.list_library_files(["public"])

        sql, params = log[0]
        # 同名文件聚在一起、新版在前：面板打开「显示历史版本」时 v2 紧跟着它的 v1
        assert "ORDER BY scope, file_name, version DESC, id" in sql
        assert "is_archived = 0" in sql
        assert params == ("public",)

    def test_create_pending_falls_back_to_max_plus_one_for_legacy_callers(self, monkeypatch):
        """老调用方（Streamlit 版 app.py）不传版本号 → 服务层自己算 = 同名最新 + 1。

        不能让老入口继续写 v1：同一个 (库, 文件名) 下出现两条 v1 之后，面板上的
        版本列就彻底失去意义（两行都显示 v1，谁也分不清谁是新的）。
        """
        from services import file_state

        log, results = [], [{"file_uuid": "u-1", "version": 1}]
        monkeypatch.setattr(file_state, "get_conn", lambda: _FakeConn(log, results))

        file_state.create_pending("hash", "public", "手册.pdf", "src", "owner", "u-9")

        assert "version DESC" in log[0][0], "先查同名最新版本"
        assert log[2][1][-1] == 2, f"INSERT 的版本号应为 2，实际参数 {log[2][1]}"
        assert "version" in log[2][0]
