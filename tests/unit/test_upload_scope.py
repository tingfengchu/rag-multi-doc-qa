"""上传端点 / 管理端授权端点的**角色权限矩阵**（模块 3，纯离线）。

为什么 ``test_scope_auth.py`` 测过 ``resolve_scope`` 之后还要在端点级再测一遍：
    函数级测试只覆盖"被调用、且 write=True"这一条路径，真正会上线的两种事故它都测不到——
    ① 某个路由忘了传 ``write=True``（于是所有角色都能写公共库，而矩阵测试全绿）；
    ② 权限通过了、却因为后面别的原因 403/500（用户会误以为"换个角色就好了"）。
    所以这里用 ``TestClient`` 打真实的 multipart 上传，把 403 与 200 都钉死。

离线策略：只把重活换成替身（Docling 解析 / Chroma / ES / MySQL 状态表），
路由、依赖注入、权限判断全部走真实代码；不启 lifespan（``TestClient`` 不加 ``with``），
所以不会去连 ES。
"""
import fnmatch
import os
import types

import pytest
from fastapi.testclient import TestClient

from api.deps import get_current_user
from api.main import app
from api.routers import admin as admin_router
from api.routers import files as files_router
from config import settings
from services import file_store, redis_service
from utils.roles import ROLE_ADMIN, ROLE_AUTHORIZED_USER, ROLE_USER

USER = {"user_uuid": "uuid-alice", "username": "alice", "role": ROLE_USER}
AUTHORIZED = {"user_uuid": "uuid-bob", "username": "bob", "role": ROLE_AUTHORIZED_USER}
ADMIN = {"user_uuid": "uuid-root", "username": "root", "role": ROLE_ADMIN}


class FakeFileState:
    """``services.file_state`` 的内存替身：只记录调用，不碰 MySQL。"""

    def __init__(self):
        self.pending = []
        self.success = []
        self.failed = []
        self.deleted = []
        #: ``list_success_files`` 的返回（删除路径要按它找到目标记录）
        self.files = []
        #: 模块 5：``latest_version`` 的返回值，键是 ``(scope, file_name)``；
        #: 不预置 = 库里还没有同名文件（新文件走 v1，并仍然按内容哈希查重）
        self.latest = {}
        #: 模块 5：``create_pending`` 收到的版本号（单独一份，免得改动 pending 的元组形状）
        self.pending_versions = []
        #: 模块 5：``archive_versions`` 收到的调用
        self.archived = []

    def check_duplicate(self, file_hash, scope):
        return False

    def create_pending(self, file_hash, scope, file_name, source, user_uuid, file_uuid, version=None):
        self.pending.append((file_uuid, scope, file_name))
        self.pending_versions.append(version)

    def latest_version(self, scope, file_name):
        return self.latest.get((scope, file_name))

    def archive_versions(self, scope, file_name, keep_file_uuid):
        self.archived.append((scope, file_name, keep_file_uuid))
        return 1

    def mark_success(self, file_uuid, scope):
        self.success.append((file_uuid, scope))

    def mark_failed(self, file_uuid, scope):
        self.failed.append((file_uuid, scope))

    def list_success_files(self, scope, include_archived=False):
        return [row for row in self.files if row["scope"] == scope]

    def delete_record(self, file_uuid, scope):
        self.deleted.append((file_uuid, scope))

    def get_file(self, file_uuid):
        """按 uuid 取一条记录（``file_state.get_file`` 的替身：下载 / 预览要用）。"""
        for row in self.files:
            if row["file_uuid"] == file_uuid:
                return dict(row)
        return None


class FakePdfService:
    """不跑 Docling：直接给出"父块 1 / 子块 1"的最小合法结构。"""

    def process_uploaded_document_with_parent_child(
        self, uploaded, user_uuid, file_uuid, source
    ):
        meta = {"parent_id": "p1", "page": 1, "Header 1": "标题"}
        return {
            "parents": [types.SimpleNamespace(page_content="父块", metadata=dict(meta))],
            "children": [types.SimpleNamespace(page_content="子块", metadata=dict(meta))],
        }


class FakeVectorService:
    def __init__(self):
        self.deleted_sources = []

    def get_vectorstore(self, scope):
        return f"chroma:{scope}"

    def add_documents(self, vectorstore, children):
        return None

    def delete_by_source(self, vectorstore, source):
        self.deleted_sources.append(source)


class FakeEsService:
    def __init__(self):
        self.deleted_file_uuids = []

    def index_parent_chunk(self, *args, **kwargs):
        return None

    def index_child_chunk(self, *args, **kwargs):
        return None

    def refresh_index(self):
        return None

    def delete_by_file_uuid(self, file_uuid, scope):
        self.deleted_file_uuids.append((file_uuid, scope))


class FakeFileStore:
    """``services.file_store`` 的内存替身：记录"存了哪些原件"，不真的落盘。

    为什么必须在夹具里替换掉它（而不是让它真写）：上传用例会跑几十次真实 multipart 上传，
    真实现会在项目里留下 ``file_store/<随机 uuid>.pdf`` —— 测试产物污染数据目录，
    而且没人会想到去那里清理。
    """

    def __init__(self):
        self.saved = []
        self.removed = []

    def save_original(self, file_uuid, filename, content):
        self.saved.append((file_uuid, filename, content))
        return f"memory://{file_uuid}"

    def remove_original(self, file_uuid, filename):
        self.removed.append((file_uuid, filename))
        return True


class _CacheRedis:
    """语义缓存那一小块 Redis 的内存替身：``set`` / ``scan_iter`` / ``get`` / ``delete``。

    与 ``test_semantic_cache.py:FakeRedis`` 的分工：那边是**匹配逻辑**的试验台（自己造各种
    脏 payload）；这里只是让端点里的**真实现**跑起来，用来断言"删完缓存条目真的不在了"。
    所以故意只留最小壳子 —— 多一个方法就多一处替身可以撒谎的地方。
    """

    def __init__(self, events=None):
        self.strings = {}
        #: 真被 ``delete`` 掉的顺序（顺序用例拿它跟 ES / Chroma 的动作比先后）
        self.events = events if events is not None else []
        #: 模拟 Redis 不可用：``scan_iter`` 一调就炸（删除接口必须因此整体失败）
        self.fail_on_scan = False

    def set(self, key, value, ex=None, **kwargs):
        self.strings[key] = value
        return True

    def scan_iter(self, match=None):
        # 生成器体在**首次取值**时才执行，所以这里的 raise 会在
        # `list(r.scan_iter(...))` 那一刻抛出 —— 正是要复现的"Redis 连不上"。
        if self.fail_on_scan:
            raise ConnectionError("redis 连接失败")
        for key in list(self.strings):
            if fnmatch.fnmatch(key, match or "*"):
                yield key

    def get(self, key):
        return self.strings.get(key)

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)
        self.events.append("cache")
        return len(keys)


@pytest.fixture()
def file_state_double(monkeypatch):
    fake = FakeFileState()
    monkeypatch.setattr(files_router, "file_state", fake)
    monkeypatch.setattr(files_router, "pdf_service", FakePdfService())
    monkeypatch.setattr(files_router, "vector_service", FakeVectorService())
    monkeypatch.setattr(files_router, "es_service", FakeEsService())
    monkeypatch.setattr(files_router, "file_store", FakeFileStore())
    return fake


@pytest.fixture()
def file_store_double(file_state_double, monkeypatch):
    """单独取原件留存替身（上传成功路径要断言"原件被留了一份"）。

    依赖 ``file_state_double`` 是**必须的**：那个夹具也会替换 ``file_store``，
    先声明依赖才能保证本夹具的替换发生在后面（否则两个夹具的生效顺序不确定）。
    """
    double = FakeFileStore()
    monkeypatch.setattr(files_router, "file_store", double)
    return double



@pytest.fixture()
def client(file_state_double):
    """不带 ``with`` 的 TestClient：不触发 lifespan（本文件不需要 ES 自检）。"""
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def _as(user):
    """覆盖 ``get_current_user``：模拟"已登录的某种角色"。

    刻意不碰 HTTP/Redis 会话层——那一层已由 ``test_sessions.py`` 与集成测试覆盖，
    这里要单独验证的是"角色 → 能不能写哪个 scope"。
    """
    app.dependency_overrides[get_current_user] = lambda: user


def _upload(client, scope):
    return client.post(
        "/api/files/upload",
        data={"scope": scope},
        files={"file": ("demo.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )


def _raise_oserror(*args, **kwargs):
    """替身用的"磁盘写失败"（``file_store.save_original`` 只会抛 ``OSError``）。"""
    raise OSError("磁盘满了")


def _raise_value_error(*args, **kwargs):
    """替身用的"解析失败"（``pdf_service`` 失败时抛任意异常，路由统一翻成 400）。"""
    raise ValueError("Docling 未能从该文档中提取到任何有效文本。")


class TestUploadWritePermissions:
    """上传的写权限矩阵（模块 3 的验收点）。"""

    def test_plain_user_cannot_upload_to_public(self, client):
        """"user 上传 public → 403"（验收清单：普通用户不能写公共库）。"""
        _as(USER)
        resp = _upload(client, "public")

        assert resp.status_code == 403
        # 文案要告诉用户"可以找管理员授权"，否则 authorized_user 这个角色等于不存在
        assert "授权" in resp.json()["detail"]

    def test_admin_can_upload_to_public(self, client):
        """"admin 上传 public → 200"（验收清单：管理员可以写公共库）。"""
        _as(ADMIN)
        resp = _upload(client, "public")

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        assert resp.json()["file_uuid"]

    def test_authorized_user_can_upload_to_public(self, client):
        """模块 3 的核心新增能力：被授权用户能写公共库（但见下一个类：他不是管理员）。"""
        _as(AUTHORIZED)
        assert _upload(client, "public").status_code == 200

    def test_plain_user_can_upload_to_own_private(self, client):
        """普通用户并不是"什么都不能传"：私人库照旧可用（别把功能收紧过头）。"""
        _as(USER)
        assert _upload(client, f"private:{USER['user_uuid']}").status_code == 200

    def test_authorized_user_can_upload_to_own_private(self, client):
        _as(AUTHORIZED)
        assert _upload(client, f"private:{AUTHORIZED['user_uuid']}").status_code == 200

    def test_authorized_user_cannot_write_others_private(self, client):
        """被授权写公共库 ≠ 能翻别人私人库：模块 1 立下的隐私红线必须原样保住。"""
        _as(AUTHORIZED)
        assert _upload(client, "private:uuid-alice").status_code == 403

    def test_admin_cannot_write_others_private_either(self, client):
        _as(ADMIN)
        assert _upload(client, "private:uuid-alice").status_code == 403

    def test_upload_lands_in_server_resolved_scope(self, client, file_state_double):
        """入库必须落到**服务端解析出的** scope，而不是客户端传来的原串。"""
        _as(USER)
        _upload(client, f"  private:{USER['user_uuid']}  ")

        assert [scope for _, scope in file_state_double.success] == [
            f"private:{USER['user_uuid']}"
        ]

    def test_rejected_upload_touches_no_state(self, client, file_state_double):
        """403 必须在**动任何状态之前**返回：否则会留下 pending 记录与孤儿文件。"""
        _as(USER)
        _upload(client, "public")

        assert file_state_double.pending == []
        assert file_state_double.success == []


#: 最小 PNG 头（前 8 字节就是 PNG 魔数）。内容本身无关紧要：拒绝发生在读内容之前。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-body" * 4
PNG_NAME = "界面截图.png"
PNG_MIME = "image/png"


class TestImageUploadIsRejected:
    """决策：**暂不支持图片**（真图进 Docling 只会得到"未提取到有效文本"，白等几十秒）。

    为什么要在端点级钉死这句 400：接口是唯一入口。前端把图片从 ``accept`` 里拿掉只是
    "少给一个选择"，绕过界面直接调接口的人（脚本 / 缓存里的老前端）必须拿到明确文案，
    而不是等几十秒再收到一句看不懂的"入库失败"。
    """

    def _post(self, client, name, mime, content=PNG_BYTES):
        return client.post(
            "/api/files/upload",
            data={"scope": "public"},
            files={"file": (name, content, mime)},
        )

    @pytest.mark.parametrize(
        "name",
        ["截图.png", "照片.JPG", "photo.jpeg", "anim.gif", "pic.webp", "扫描件.tif"],
    )
    def test_image_extension_is_rejected(self, client, name):
        """后端认**扩展名**：不管 MIME 报什么，``.png`` 这类一律 400（大小写不敏感）。"""
        _as(ADMIN)
        resp = self._post(client, name, "application/octet-stream")

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "图片格式暂不支持上传"

    @pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "IMAGE/PNG"])
    def test_image_mime_is_rejected_even_under_a_document_name(self, client, mime):
        """改个扩展名也不放行：声明的 ``image/*`` 同样判图片（**类型前缀**大小写不敏感）。"""
        _as(ADMIN)
        resp = self._post(client, "伪装成报告.docx", mime)

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "图片格式暂不支持上传"

    def test_rejected_image_touches_no_state(
        self, client, file_state_double, file_store_double
    ):
        """拒绝要发生在**动状态之前**：否则每误传一次都留一条 pending + 一份无人认领的原件。"""
        _as(ADMIN)
        self._post(client, PNG_NAME, PNG_MIME)

        assert file_state_double.pending == []
        assert file_state_double.success == []
        assert file_store_double.saved == []

    def test_document_is_still_accepted(self, client):
        """别把口子收过头：决策只针对图片，普通文档（含大写扩展名）照旧 200。"""
        _as(ADMIN)
        resp = self._post(
            client,
            "季度报告.DOCX",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04fake-docx-body" * 10,
        )

        assert resp.status_code == 200, resp.text


class TestAdminRoleEndpointAuth:
    """``PUT /api/admin/users/{uuid}/role`` 的权限边界。"""

    def _put(self, client, role="authorized_user", user_uuid="uuid-bob"):
        return client.put(f"/api/admin/users/{user_uuid}/role", json={"role": role})

    def test_plain_user_is_forbidden(self, client):
        _as(USER)
        assert self._put(client).status_code == 403

    def test_authorized_user_is_forbidden(self, client):
        """被授权写公共库 ≠ 管理员：授权能力本身绝不能下放（否则人人可自封管理员）。"""
        _as(AUTHORIZED)
        assert self._put(client).status_code == 403

    def test_users_list_is_admin_only(self, client):
        _as(AUTHORIZED)
        assert client.get("/api/admin/users").status_code == 403

    def test_unknown_role_rejected_by_schema(self, client):
        """角色白名单在进业务层之前就由 ``Literal`` 挡住（422，而不是 403 / 500）。"""
        _as(ADMIN)
        assert self._put(client, role="superuser").status_code == 422


class TestAdminRoleEndpointRules:
    """成功路径 + 业务拒绝的状态码映射（``auth.set_user_role`` 的真库访问换成替身）。"""

    @pytest.fixture()
    def doubles(self, monkeypatch):
        calls = {"role": [], "revoked": []}

        def fake_set_role(user_uuid, role, operator_uuid=None):
            calls["role"].append((user_uuid, role, operator_uuid))
            return True, "ok"

        def fake_get_user(user_uuid):
            return {
                "user_uuid": user_uuid,
                "username": "bob",
                "role": ROLE_AUTHORIZED_USER,
                "is_active": 1,
                "created_at": None,
            }

        monkeypatch.setattr(admin_router, "set_user_role", fake_set_role)
        monkeypatch.setattr(admin_router, "get_user", fake_get_user)
        monkeypatch.setattr(
            admin_router.sessions,
            "revoke_all_sessions",
            lambda user_uuid: calls["revoked"].append(user_uuid) or 2,
        )
        return calls

    def test_admin_can_grant_write_public(self, client, doubles):
        _as(ADMIN)
        resp = client.put(
            "/api/admin/users/uuid-bob/role", json={"role": ROLE_AUTHORIZED_USER}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == ROLE_AUTHORIZED_USER
        # 操作者必须来自**会话**（请求体里没有身份字段），否则可以冒充别的管理员
        assert doubles["role"] == [("uuid-bob", ROLE_AUTHORIZED_USER, ADMIN["user_uuid"])]

    def test_role_change_revokes_target_sessions(self, client, doubles):
        """改完立刻吊销对方会话：否则降级要等对方重新登录才生效。"""
        _as(ADMIN)
        client.put("/api/admin/users/uuid-bob/role", json={"role": ROLE_USER})

        assert doubles["revoked"] == ["uuid-bob"]

    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("不能修改自己的角色，请让另一位管理员操作", 400),
            ("这是最后一名在册管理员，不能改成其它角色", 400),
            ("用户不存在", 404),
        ],
    )
    def test_business_rejection_maps_to_status(self, client, monkeypatch, msg, expected):
        monkeypatch.setattr(admin_router, "set_user_role", lambda *a, **kw: (False, msg))
        monkeypatch.setattr(admin_router.sessions, "revoke_all_sessions", lambda u: 0)
        _as(ADMIN)

        resp = client.put("/api/admin/users/uuid-bob/role", json={"role": ROLE_USER})

        assert resp.status_code == expected
        assert resp.json()["detail"] == msg


class TestOriginalFileIsKept:
    """上传时必须把**原件**留一份：``GET /files/{uuid}/download`` 全指望它。

    改造前没有任何地方保存原件（``pdf_service`` 解析完就把 tempfile 删了），
    所以这组用例锁的是新增的落盘行为，以及它与"入库失败"的先后关系 ——
    顺序错了就会出现"入库了却永远下不到原件"，或"入库失败还留着无人认领的原件"。
    """

    def test_original_bytes_are_kept_with_the_response_uuid(self, client, file_store_double):
        _as(ADMIN)
        resp = _upload(client, "public")

        assert resp.status_code == 200, resp.text
        assert len(file_store_double.saved) == 1
        saved_uuid, saved_name, saved_bytes = file_store_double.saved[0]
        assert saved_uuid == resp.json()["file_uuid"], "留存件必须挂在同一个 file_uuid 上，否则下载找不到"
        assert saved_name == "demo.pdf", "要记下原始文件名（下载时 Content-Disposition 用它）"
        assert saved_bytes == b"%PDF-1.4 fake", "必须是逐字节的原件"

    def test_rejected_upload_saves_nothing(self, client, file_store_double):
        """403 在动任何状态之前返回 → 留存目录里也不该多出文件。"""
        _as(USER)
        _upload(client, "public")

        assert file_store_double.saved == []

    def test_duplicate_upload_does_not_save_again(self, client, file_store_double, file_state_double):
        """重复上传直接返回"已入库" → 不该再留一份。"""
        _as(ADMIN)
        file_state_double.check_duplicate = lambda file_hash, scope: True

        assert _upload(client, "public").json()["duplicate"] is True
        assert file_store_double.saved == []

    def test_save_failure_fails_the_upload_before_indexing(
        self, client, file_store_double, file_state_double
    ):
        """留存失败要**挡在入库之前**：否则会留下"入库了却永远下不到原件"的记录。"""
        _as(ADMIN)
        file_store_double.save_original = _raise_oserror
        resp = _upload(client, "public")

        assert resp.status_code == 400
        assert "原件留存失败" in resp.json()["detail"]
        assert file_state_double.pending == [], "留存都失败了，不该已经写进 pending"
        assert file_state_double.success == []

    def test_indexing_failure_removes_the_kept_original(
        self, client, file_store_double, file_state_double, monkeypatch
    ):
        """入库失败 → 留存件一起清掉：DB 记录是 failed，那份件永远不可达。"""
        _as(ADMIN)
        monkeypatch.setattr(
            files_router,
            "pdf_service",
            types.SimpleNamespace(
                process_uploaded_document_with_parent_child=_raise_value_error
            ),
        )
        resp = _upload(client, "public")

        assert resp.status_code == 400
        assert "入库失败" in resp.json()["detail"]
        assert len(file_store_double.saved) == 1
        assert file_store_double.removed == [(file_store_double.saved[0][0], "demo.pdf")]
        assert file_state_double.failed, "失败状态要如实记录，便于用户重试"

    def test_deleting_a_file_also_drops_the_kept_original(
        self, client, file_store_double, file_state_double
    ):
        """删除文件要连留存原件一起删，否则 file_store/ 会越用越大（且那些件已不可达）。"""
        _as(ADMIN)
        file_state_double.files = [
            {
                "file_uuid": "uuid-1",
                "file_name": "demo.pdf",
                "scope": "public",
                "source": "root__uuid-1__demo.pdf",
            }
        ]
        resp = client.delete("/api/files/uuid-1", params={"scope": "public"})

        assert resp.status_code == 200, resp.text
        assert file_store_double.removed == [("uuid-1", "demo.pdf")]
        assert file_state_double.deleted == [("uuid-1", "public")]


#: 与 ``test_file_download.py`` 同一份"最小 Office 文件"：前两字节 ``PK`` 才是能打开的 xlsx。
XLSX_BYTES = b"PK\x03\x04" + b"fake-sheet-body" * 20
XLSX_NAME = "模板信息表 工号.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestUploadedXlsxCanBeDownloadedWhole:
    """「上传 xlsx → 下载」整条链路必须逐字节闭环（用户报的「Excel 下载失败」就出在这里）。

    与 ``test_file_download.py:TestOfficeOriginalIsServedWhole`` 的分工：
    这里走**真实上传端点**（multipart → 留存原件 → 入库 → 再下载），
    验证"上传时留在磁盘上的那份件，能不能原样送出去"；那边只测下载侧的长度口径，
    覆盖更窄、定位更快。两条盯着同一条不变量：**声明长度 == 真实发出去的字节数**。

    离线策略与本文件其它用例一致：解析 / 向量 / ES / 状态表全是替身，
    **只有 ``file_store`` 是真的**（写 tmp_path）—— 要断言的正是"磁盘上那份件"。
    长度口径的坑（本机 ``.xlsx`` 的 ``os.stat`` 多报 7582 字节，见 ``tests/conftest.py``）
    由 ``lying_stat`` 夹具确定性复现，所以这条不变量在任何机器上都守得住。
    """

    @pytest.fixture()
    def real_store(self, file_state_double, monkeypatch, tmp_path):
        """把 ``files.py`` 里的 ``file_store`` 换回**真实现**，目录指到 tmp_path。

        为什么显式依赖 ``file_state_double``：那个夹具会把 ``file_store`` 换成内存替身，
        本夹具必须在它**之后**生效（依赖关系保证了"先声明后被覆盖"的顺序）。
        """
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        monkeypatch.setattr(settings, "file_store_dir", str(tmp_path / "store"))
        monkeypatch.setattr(settings, "file_legacy_search_dirs", str(legacy))
        monkeypatch.setattr(files_router, "file_store", file_store)
        return tmp_path / "store"

    def test_uploaded_xlsx_downloads_byte_exact(
        self, client, file_state_double, real_store, lying_stat
    ):
        _as(ADMIN)
        up = client.post(
            "/api/files/upload",
            data={"scope": "public"},
            files={"file": (XLSX_NAME, XLSX_BYTES, XLSX_MIME)},
        )

        assert up.status_code == 200, up.text
        file_uuid = up.json()["file_uuid"]
        stored = os.path.join(str(real_store), f"{file_uuid}.xlsx")
        assert os.path.isfile(stored), "上传成功就必须留一份原件，下载全靠它"
        assert os.path.getsize(stored) == len(XLSX_BYTES) + lying_stat, (
            "前提检查：本机对 .xlsx 的 stat 会多报（夹具把它复现出来了）"
        )

        # 下载按 file_uuid 取记录：把这次上传的结果补进替身（等价于 MySQL 里刚写好的那行）
        file_state_double.files = [
            {
                "file_uuid": file_uuid,
                "file_name": XLSX_NAME,
                "scope": "public",
                "owner_uuid": ADMIN["user_uuid"],
                "source": f"{ADMIN['user_uuid']}__{file_uuid}__{XLSX_NAME}",
            }
        ]
        resp = client.get(f"/api/files/{file_uuid}/download")

        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == XLSX_MIME, "MIME 由扩展名推导，Excel 据此识别"
        assert resp.content[:2] == b"PK", "前两字节是 PK 才是能打开的 Office 文件"
        assert resp.content == XLSX_BYTES, "原件逐字节一致：不是截断，也不是重新编码"
        assert int(resp.headers["content-length"]) == len(resp.content), (
            "声明长度 = 真实发出去的字节数；照抄 os.stat 就会声明 20480、只发 12898"
        )


class TestDeleteDropsTheCachedAnswersThatCitedTheFile:
    """删除文件 → 引用它的语义缓存必须**立刻**失效（2026-10-14 报的事故）。

    事故现场：删除只清了 ES / Chroma / MySQL 记录，缓存里那条答案还在 → 用户再问同样的
    问题就命中缓存 → 回答继续引用已删除的文件 → 点引用下载 404。

    端点级断言的理由：``invalidate_cache_by_file_uuid`` 的匹配逻辑由
    ``tests/unit/test_semantic_cache.py`` 覆盖；这里要钉死的是**删除接口真的调了它、
    而且调在"文件还活着"的时候** —— 少了这一步，那个函数再对也没人去用。
    """

    # 端点级只看"缓存条目删没删"这一个事实：匹配逻辑的粒度（子串/脏数据/TTL）由
    # test_semantic_cache.py 负责。反过来，那边不测"删除接口有没有真的调用它"。
    FILE_ROW = {
        "file_uuid": "uuid-1",
        "file_name": "demo.pdf",
        "scope": "public",
        "source": "root__uuid-1__demo.pdf",
    }

    #: 缓存里的问题（与 FILE_ROW 无关，只是要一个稳定的 key）
    QUESTION = "这份文件讲的是哪个项目"

    @pytest.fixture()
    def cache(self, monkeypatch):
        """换上内存 Redis，并预置一条**引用了待删文件**的语义缓存。

        刻意**不**替换 ``invalidate_cache_by_file_uuid``（本文件旧版本正是那么写的：一个
        只记录调用的替身，于是这 6 条用例对「把那个函数换成空实现」完全无感 ——
        2026-10-14 用 no-op 插件复核时全绿）。这里让端点调用**真实现**，
        断言缓存条目真的从 Redis 里消失。

        预置走 ``set_cached_answer`` 而不是手写 JSON：缓存长什么样永远由生产代码决定，
        测试不会再抄一份会漂移的格式。
        """
        client = _CacheRedis()
        monkeypatch.setattr(redis_service, "r", client)
        redis_service.set_cached_answer(
            "public",
            self.QUESTION,
            "旧答案",
            [{"source": "demo.pdf", "page": 1, "file_uuid": self.FILE_ROW["file_uuid"]}],
            [],
            rerank_score=0.9,
        )
        key = redis_service._build_cache_key("public", self.QUESTION, "all")
        assert key in client.strings, "前提：这条缓存确实写进去了"
        return client, key

    def _one_public_file(self, file_state_double):
        file_state_double.files = [dict(self.FILE_ROW)]

    def _delete(self, client, file_uuid=None, scope="public"):
        return client.delete(
            f"/api/files/{file_uuid or self.FILE_ROW['file_uuid']}", params={"scope": scope}
        )

    def test_delete_removes_the_cached_answer_that_cited_the_file(
        self, client, file_state_double, cache
    ):
        _as(ADMIN)
        self._one_public_file(file_state_double)
        redis_double, key = cache

        resp = self._delete(client)

        assert resp.status_code == 200, resp.text
        # 真行为断言：只断言"函数被调用过"不够 —— 换成空实现那条断言照样绿。
        assert key not in redis_double.strings, (
            "删除文件后那条引用它的缓存必须真的消失：否则用户再问同样的问题会命中旧答案、"
            "回答继续引用已删除的文件 → 点引用下载 404"
        )

    def test_delete_still_does_everything_it_used_to(
        self, client, file_state_double, file_store_double, cache
    ):
        """回归：新增这一步不许改变原有删除语义（ES / Chroma / 记录 / 留存原件照样清）。"""
        _as(ADMIN)
        self._one_public_file(file_state_double)
        redis_double, key = cache

        resp = self._delete(client)

        assert resp.status_code == 200, resp.text
        assert file_state_double.deleted == [("uuid-1", "public")]
        assert file_store_double.removed == [("uuid-1", "demo.pdf")]
        assert key not in redis_double.strings

    def test_denied_delete_never_touches_the_cache(self, client, file_state_double, cache):
        """403（普通用户删公共库）→ 一个缓存都不许动：越权请求不能顺手清库房。"""
        _as(USER)
        self._one_public_file(file_state_double)
        redis_double, key = cache

        assert self._delete(client).status_code == 403
        assert key in redis_double.strings
        assert file_state_double.deleted == []

        # 对照组：同一条删除由管理员发起时必须真的清掉缓存。
        # 少了这一半，"403 没动缓存"在失效函数是空实现时也成立。
        _as(ADMIN)
        assert self._delete(client).status_code == 200
        assert key not in redis_double.strings

    def test_unknown_file_never_touches_the_cache(self, client, file_state_double, cache):
        """404（uuid 不存在）→ 不去扫缓存：否则任何人都能拿随机 uuid 让全库缓存被扫一遍。"""
        _as(ADMIN)
        self._one_public_file(file_state_double)
        redis_double, key = cache

        assert self._delete(client, file_uuid="uuid-nope").status_code == 404
        assert key in redis_double.strings, "404 在动状态之前就返回了，缓存一个都不许动"

        # 对照组：真实的 uuid → 缓存消失（否则"404 没动缓存"在空实现下也成立）
        assert self._delete(client).status_code == 200
        assert key not in redis_double.strings

    def test_cache_is_invalidated_before_the_file_disappears(
        self, client, file_state_double, monkeypatch, cache
    ):
        """顺序：先失效缓存 → 再动 ES / Chroma / 记录 / 原件。

        反过来（放最后）的话，一次 Redis 故障就会留下"文件已删、缓存还在"，
        用户点引用照样 404 —— 也就是这次要修的 bug 原样复现。
        """
        _as(ADMIN)
        self._one_public_file(file_state_double)
        redis_double, key = cache
        order = redis_double.events          # 替身真被 DEL 时才会写进 "cache"
        assert order == [], "前提：此刻还没有任何缓存被删"

        monkeypatch.setattr(
            files_router.es_service,
            "delete_by_file_uuid",
            lambda file_uuid, scope: order.append("es"),
        )
        monkeypatch.setattr(
            files_router.vector_service,
            "delete_by_source",
            lambda vectorstore, source: order.append("chroma"),
        )

        self._delete(client)

        # 顺序断言必须建立在"缓存真的被删了"之上：把失效函数换成空实现时，
        # `order` 里连 "cache" 都没有，这条用例会红（旧版本只看替身被调用过 → 照样绿）。
        assert order[0] == "cache", "缓存没被真删 —— 顺序断言不能只看替身被调用过"
        assert order.index("cache") < order.index("es"), "缓存失效必须排在真正的删除动作之前"
        assert key not in redis_double.strings

    def test_redis_failure_aborts_before_anything_is_deleted(
        self, client, file_state_double, file_store_double, cache
    ):
        """Redis 不可用 → 整次删除失败、文件原封不动（用户重试即可）。

        用 ``raise_server_exceptions=False`` 的 client：这里要看的正是**用户拿到的 500**，
        而不是 pytest 内部被重新抛出的那个异常。
        """
        _as(ADMIN)
        self._one_public_file(file_state_double)
        redis_double, key = cache
        # 故障用**真实现**制造（替身的 scan_iter 一调就抛），而不是把失效函数换成会抛的
        # 替身 —— 后者只能证明"路由没吞异常"，而且在 no-op 复核下照样绿（空实现不会抛）。
        redis_double.fail_on_scan = True

        c = TestClient(app, raise_server_exceptions=False)

        resp = c.delete("/api/files/uuid-1", params={"scope": "public"})

        assert resp.status_code == 500
        assert file_state_double.deleted == [], "缓存没清干净就把记录删了 —— 事故会原样复现"
        assert file_store_double.removed == []
        assert key in redis_double.strings


