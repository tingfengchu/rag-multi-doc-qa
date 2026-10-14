"""``GET /api/files/{file_uuid}/download`` / ``/preview`` 的权限矩阵（端点级、离线）。

为什么要在端点级测这套矩阵（``deps.resolve_scope`` 的单测覆盖不到它）：
下载接口的权限判断**不在 scope 字符串上**，而在"记录里的 scope + 会话用户"两者之间：

* ``public``            → 任何已登录用户都能下；
* ``private:<owner>``   → 只有 owner 本人；别人（**包括管理员**）403；
* 记录不存在 / 原件丢失 → 404，且两者文案不同。

这三条里最危险的是"scope 过滤没命中就 404"：界面上刚看到过的引用点下载却说"文件不存在"，
用户只会以为系统坏了。所以下面把 403 与 404 分开钉死。

另外把 ``Content-Disposition`` 也测了：中文文件名靠 Starlette 的 RFC 5987 编码
（``filename*=utf-8''…``），这是"下下来是乱码名/直接报错"的常见事故点。

离线策略：真 ``file_store``（写到 tmp_path，链路完整）+ 替身 ``file_state``（不连 MySQL）；
``TestClient`` 不加 ``with``，不触发 lifespan。
"""
import os

import pytest
from fastapi.testclient import TestClient

from api.deps import get_current_user
from api.main import app
from api.routers import files as files_router
from config import settings
from services import file_store
from utils.roles import ROLE_ADMIN, ROLE_USER

ALICE = {"user_uuid": "uuid-alice", "username": "alice", "role": ROLE_USER}
BOB = {"user_uuid": "uuid-bob", "username": "bob", "role": ROLE_USER}
ROOT = {"user_uuid": "uuid-root", "username": "root", "role": ROLE_ADMIN}

PUBLIC_UUID = "11111111-1111-4111-8111-111111111111"
ALICE_PRIVATE_UUID = "22222222-2222-4222-8222-222222222222"
BOB_PRIVATE_UUID = "33333333-3333-4333-8333-333333333333"
GHOST_UUID = "44444444-4444-4444-8444-444444444444"
NO_ORIGINAL_UUID = "55555555-5555-4555-8555-555555555555"
#: 浏览器**能当页面渲染**的类型（HTML）：预览必须降级成 attachment，
#: 理由见 ``TestPreviewEndpoint.test_renderable_types_are_downgraded_to_attachment``。
HTML_UUID = "66666666-6666-4666-8666-666666666666"
HTML_NAME = "上传的页面.html"

#: 中文 + 空格 + 括号：最能暴露 header 编码问题的那种文件名
PUBLIC_NAME = "SAGE AI 智能问答助手.pdf"
PDF_BYTES = b"%PDF-1.4\n% test\n"


def _row(file_uuid, file_name, scope, owner_uuid):
    return {
        "file_uuid": file_uuid,
        "file_name": file_name,
        "scope": scope,
        "owner_uuid": owner_uuid,
        "source": f"{owner_uuid}__{file_uuid}__{file_name}",
        "created_at": None,
        "updated_at": None,
    }


ROWS = [
    _row(PUBLIC_UUID, PUBLIC_NAME, "public", ROOT["user_uuid"]),
    _row(ALICE_PRIVATE_UUID, "我的简历 (AI应用开发)(3).pdf", f"private:{ALICE['user_uuid']}", ALICE["user_uuid"]),
    _row(BOB_PRIVATE_UUID, "bob 的私人文档.pdf", f"private:{BOB['user_uuid']}", BOB["user_uuid"]),
    # 有记录、没原件（改造前上传的文件就是这个状态）
    _row(NO_ORIGINAL_UUID, "改造前上传的旧文件.pdf", "public", ROOT["user_uuid"]),
    # 原件在、但类型不允许内嵌（HTML 能被浏览器当页面渲染）
    _row(HTML_UUID, HTML_NAME, "public", ROOT["user_uuid"]),
]


class FakeFileState:
    """``services.file_state`` 的内存替身：只实现下载路径用到的那一个函数。"""

    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def get_file(self, file_uuid):
        self.asked.append(file_uuid)
        for row in self.rows:
            if row["file_uuid"] == file_uuid:
                return dict(row)
        return None


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """真 ``file_store`` + 临时目录：不碰项目根目录，也不受"本机有没有那份原件"影响。"""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(settings, "file_store_dir", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "file_legacy_search_dirs", str(legacy))
    file_store.save_original(PUBLIC_UUID, PUBLIC_NAME, PDF_BYTES)
    file_store.save_original(ALICE_PRIVATE_UUID, "我的简历 (AI应用开发)(3).pdf", b"private-a")
    file_store.save_original(BOB_PRIVATE_UUID, "bob 的私人文档.pdf", b"private-b")
    file_store.save_original(HTML_UUID, HTML_NAME, b"<html><body>hi</body></html>")
    return tmp_path


@pytest.fixture()
def file_state_double(monkeypatch, store):
    fake = FakeFileState(ROWS)
    monkeypatch.setattr(files_router, "file_state", fake)
    return fake


@pytest.fixture()
def client(file_state_double):
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user


class TestPublicScope:
    """公共库：登录即可下载（普通用户也算"登录"）。"""

    @pytest.mark.parametrize("user", [ALICE, BOB, ROOT])
    def test_any_logged_in_user_can_download(self, client, user):
        _as(user)
        resp = client.get(f"/api/files/{PUBLIC_UUID}/download")

        assert resp.status_code == 200, resp.text
        assert resp.content == PDF_BYTES, "必须原样回传原件字节"

    def test_filename_uses_rfc5987_for_non_ascii(self, client):
        """中文名走 ``filename*=utf-8''…``：否则浏览器要么乱码、要么直接丢文件名。"""
        _as(ALICE)
        disposition = client.get(f"/api/files/{PUBLIC_UUID}/download").headers[
            "content-disposition"
        ]

        assert "attachment" in disposition
        assert "filename*=utf-8''" in disposition
        assert "SAGE%20AI" in disposition, "文件名必须经过 URL 编码（空格 → %20）"

    def test_mime_type_comes_from_the_extension(self, client):
        _as(ALICE)

        assert (
            client.get(f"/api/files/{PUBLIC_UUID}/download").headers["content-type"]
            == "application/pdf"
        )

    def test_response_is_not_cacheable(self, client):
        """私人库也可能用这个端点 → 不许中间层缓存（Cache-Control: private, no-store）。"""
        _as(ALICE)

        assert (
            client.get(f"/api/files/{PUBLIC_UUID}/download").headers["cache-control"]
            == "private, no-store"
        )

    def test_requires_login(self, client):
        """没有会话 → 401（不是 403）：先把"你是谁"问清楚，再谈"能不能下"。"""
        resp = client.get(f"/api/files/{PUBLIC_UUID}/download")

        assert resp.status_code == 401
        assert PDF_BYTES not in resp.content


class TestPrivateScope:
    """私人库：只有本人。管理员也不例外——能力边界与 ``deps.resolve_scope`` 一致。"""

    def test_owner_can_download_own_file(self, client):
        _as(ALICE)
        resp = client.get(f"/api/files/{ALICE_PRIVATE_UUID}/download")

        assert resp.status_code == 200, resp.text
        assert resp.content == b"private-a"

    def test_other_user_is_forbidden_with_actionable_message(self, client):
        _as(ALICE)
        resp = client.get(f"/api/files/{BOB_PRIVATE_UUID}/download")

        assert resp.status_code == 403, "无权 ≠ 不存在：报 404 会让用户以为文件被删了"
        assert resp.json()["detail"] == "无下载权限，请联系管理员"
        assert b"private-b" not in resp.content, "拒绝时绝不能把内容漏出去"

    def test_admin_is_also_forbidden(self, client):
        """**故意**不给管理员开这个口子：管理端看不到别人的私人库，
        放行只会造出一条"知道 uuid 就能读别人文档"的通道。"""
        _as(ROOT)

        assert client.get(f"/api/files/{BOB_PRIVATE_UUID}/download").status_code == 403

    def test_owner_check_uses_session_uuid_not_client_input(self, client):
        """客户端能构造的字符串一律不参与判断：接口根本不收 scope 参数，比对的是记录 owner 与会话 uuid。"""
        _as(ALICE)
        resp = client.get(
            f"/api/files/{BOB_PRIVATE_UUID}/download",
            params={"scope": f"private:{ALICE['user_uuid']}"},
        )

        assert resp.status_code == 403

    def test_malformed_private_scope_is_forbidden_not_crash(self, client, monkeypatch):
        """脏数据（``private:`` 空 owner）不能让接口 500 —— 一律按"不是你的"处理。"""
        _as(ALICE)
        monkeypatch.setattr(
            files_router.file_state,
            "get_file",
            lambda uuid: _row(uuid, "x.pdf", "private:", "nobody"),
        )

        assert client.get("/api/files/any-uuid/download").status_code == 403


class TestMissingFiles:
    def test_unknown_uuid_is_404(self, client):
        _as(ALICE)
        resp = client.get(f"/api/files/{GHOST_UUID}/download")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "文件不存在或已被删除"

    def test_record_without_original_says_why(self, client):
        """有记录、没原件（改造前上传）→ 404，但文案必须说清原因，否则用户只会反复点。"""
        _as(ALICE)
        resp = client.get(f"/api/files/{NO_ORIGINAL_UUID}/download")

        assert resp.status_code == 404
        assert "未找到该文件的原件" in resp.json()["detail"]

    def test_record_lookup_happens_once_per_request(self, client, file_state_double):
        """一次下载只查一次 DB 记录（避免"先查权限、再查一遍拿文件名"的重复查询）。"""
        _as(ALICE)
        client.get(f"/api/files/{PUBLIC_UUID}/download")

        assert file_state_double.asked == [PUBLIC_UUID]


class TestPreviewEndpoint:
    """``/preview`` 与 ``/download`` 是**同一套权限、同一份原件**，只有 ``Content-Disposition`` 不同。

    为什么要在离线测试里把同一套矩阵再钉一遍（后端那边已经用 ``_row_and_original`` 共用实现了）：
    预览比下载更隐蔽——它不落盘、也不产生\"我下载了这个文件\"的动作，一旦这边少判一条，
    \"知道 uuid 就能读到别人的文档\"这条通道会重新打开而**没有任何下载记录**。
    共用实现是防错位的第一道锁，这组用例是第二道（真的从路由打进去）。
    """

    def test_public_file_previews_inline_with_the_original_bytes(self, client):
        _as(ALICE)
        resp = client.get(f"/api/files/{PUBLIC_UUID}/preview")

        assert resp.status_code == 200, resp.text
        assert resp.content == PDF_BYTES, "预览回的就是原件本身，不做任何转码"
        assert resp.headers["content-type"] == "application/pdf"

    def test_content_disposition_is_inline_not_attachment(self, client):
        """**inline 就是这个端点存在的理由**：写成 attachment 就等于下载接口。

        中文名同样要走 RFC 5987：浏览器内嵌查看器的标题栏用的就是这个名字。
        """
        _as(ALICE)
        disposition = client.get(f"/api/files/{PUBLIC_UUID}/preview").headers[
            "content-disposition"
        ]

        assert disposition.startswith("inline;"), f"必须是 inline，实际 {disposition!r}"
        assert "attachment" not in disposition
        assert "filename*=utf-8''" in disposition

    def test_renderable_types_are_downgraded_to_attachment(self, client):
        """HTML / SVG 这类能被浏览器当页面渲染的类型**不给** inline（前端拿到 attachment 就走下载）。

        blob URL 与直接导航都**继承本站 origin**：内嵌 HTML 等于让上传者在本站执行脚本。
        所以这条边界由后端把住（前端只认响应头，不自己判类型——一份规则只留一处）。
        """
        _as(ALICE)
        disposition = client.get(f"/api/files/{HTML_UUID}/preview").headers[
            "content-disposition"
        ]

        assert disposition.startswith("attachment"), f"实际 {disposition!r}"
        assert "inline" not in disposition

    def test_a_downgraded_type_still_returns_the_original_bytes(self, client):
        """降级只改 ``Content-Disposition``：原件照回（前端拿到的还是同一份文件）。"""
        _as(ALICE)
        resp = client.get(f"/api/files/{HTML_UUID}/preview")

        assert resp.status_code == 200
        assert resp.content == b"<html><body>hi</body></html>"

    @pytest.mark.parametrize("user", [ALICE, BOB, ROOT])
    def test_any_logged_in_user_can_preview_a_public_file(self, client, user):
        _as(user)

        assert client.get(f"/api/files/{PUBLIC_UUID}/preview").status_code == 200

    def test_owner_can_preview_own_private_file(self, client):
        _as(ALICE)
        resp = client.get(f"/api/files/{ALICE_PRIVATE_UUID}/preview")

        assert resp.status_code == 200, resp.text
        assert resp.content == b"private-a"

    def test_another_users_private_file_is_forbidden(self, client):
        """预览别人的私人库 → 403，且文案是\"无**预览**权限\"（不是把下载那句抄过来）。"""
        _as(ALICE)
        resp = client.get(f"/api/files/{BOB_PRIVATE_UUID}/preview")

        assert resp.status_code == 403, "无权 ≠ 不存在：报 404 会让用户以为文件被删了"
        assert resp.json()["detail"] == "无预览权限，请联系管理员"
        assert b"private-b" not in resp.content, "拒绝时绝不能把内容漏出去"

    def test_admin_is_also_forbidden_on_a_private_file(self, client):
        _as(ROOT)

        assert client.get(f"/api/files/{BOB_PRIVATE_UUID}/preview").status_code == 403

    def test_unknown_uuid_is_404(self, client):
        _as(ALICE)
        resp = client.get(f"/api/files/{GHOST_UUID}/preview")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "文件不存在或已被删除"

    def test_record_without_original_says_why(self, client):
        _as(ALICE)
        resp = client.get(f"/api/files/{NO_ORIGINAL_UUID}/preview")

        assert resp.status_code == 404
        assert "未找到该文件的原件" in resp.json()["detail"]

    def test_requires_login(self, client):
        assert client.get(f"/api/files/{PUBLIC_UUID}/preview").status_code == 401

    def test_record_lookup_happens_once_per_request(self, client, file_state_double):
        """与下载一样只查一次 DB 记录（共用实现，就该共用这个性质）。"""
        _as(ALICE)
        client.get(f"/api/files/{PUBLIC_UUID}/preview")

        assert file_state_double.asked == [PUBLIC_UUID]


#: 一份最小的"Office 文件"：**前两字节必须是 ``PK``**（ZIP/OOXML 的魔数，也是"能打开的
#: Excel"与"坏文件"之间唯一靠得住的判据）。字节长度本身无所谓，关键是原样进、原样出。
XLSX_BYTES = b"PK\x03\x04" + b"fake-sheet-body" * 20
XLSX_UUID = "77777777-7777-4777-8777-777777777777"
XLSX_NAME = "模板信息表 工号.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestOfficeOriginalIsServedWhole:
    """xlsx / docx 这类"整块二进制"原件：**声明长度必须等于真实发出去的字节数**。

    这组用例来自用户报的现象："上传 xlsx 成功，点下载浏览器说无法下载"。
    根因不是权限、不是 MIME、也不是"没按二进制读"，而是响应头里的 ``Content-Length``
    取自 ``os.stat``（``FileResponse`` 的默认行为，见 ``files.py:_original_response``），
    而本机对 ``.xlsx`` 的 stat 会多报 7582 字节（``stat = 20480`` / 真实 12898）
    → 声明 20480、实际只发得出 12898，浏览器收尾时连接断掉。
    PDF 的 stat 与真实长度恰好一致，所以同一个接口下"只有 Excel 下不了"。

    ``lying_stat`` 夹具把这个差值**确定性地**复现出来（见 ``tests/conftest.py``），
    所以这组用例在任何机器上都守得住同一条不变量：**声明长度 == 真实字节数**。
    """

    @pytest.fixture(autouse=True)
    def _xlsx_row(self, file_state_double, store, lying_stat):
        """放一份 xlsx 原件 + 一条记录（``store`` 夹具已把留存目录指到 tmp_path）。"""
        self.overreport = lying_stat
        file_store.save_original(XLSX_UUID, XLSX_NAME, XLSX_BYTES)
        file_state_double.rows.append(
            _row(XLSX_UUID, XLSX_NAME, "public", ROOT["user_uuid"])
        )

    def test_premise_stat_really_overreports(self):
        """前提检查：本用例确实处在"stat 多报"的环境里，否则它什么都守不住。"""
        assert (
            os.path.getsize(file_store.stored_path(XLSX_UUID, XLSX_NAME))
            == len(XLSX_BYTES) + self.overreport
        )

    def test_xlsx_download_is_byte_exact_and_pk(self, client):
        """上传过的 xlsx 下载回来必须是**完整**的 ZIP：200 / 正确 MIME / 前两字节 PK / 逐字节一致。"""
        _as(ALICE)
        resp = client.get(f"/api/files/{XLSX_UUID}/download")

        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == XLSX_MIME, "MIME 由扩展名推导，Excel 据此识别"
        assert resp.content[:2] == b"PK", "前两字节是 PK 才是能打开的 Office 文件"
        assert resp.content == XLSX_BYTES, "必须逐字节原样回传（二进制读，不做任何转码）"
        assert int(resp.headers["content-length"]) == len(resp.content), (
            "声明长度 = 真实发出去的字节数：照抄 os.stat 就会声明 20480、只发 12898，"
            "浏览器只会弹「失败 - 无法下载」"
        )

    def test_xlsx_preview_is_attachment_and_also_whole(self, client):
        """预览这类"不能内嵌"的类型会降级成 attachment —— 降级后的字节同样要完整。"""
        _as(ALICE)
        resp = client.get(f"/api/files/{XLSX_UUID}/preview")

        assert resp.status_code == 200, resp.text
        assert resp.headers["content-disposition"].startswith("attachment")
        assert resp.content == XLSX_BYTES
        assert int(resp.headers["content-length"]) == len(resp.content)

    def test_panel_size_is_the_readable_size_not_stat(self):
        """面板里的 ``size_bytes`` 走同一个口径：不能显示一个"下不下来的大小"。"""
        assert file_store.size_of(XLSX_UUID, XLSX_NAME) == len(XLSX_BYTES)
