"""原件留存与找回（``services/file_store``，纯离线）。

为什么这一层值得单独测：它是"下载原文"唯一的新增风险面 ——

* 路径是拼出来的（``<store>/<uuid><ext>``）：一旦让文件名参与拼接，
  ``../../`` 这种输入就是任意文件读取；
* "改造前上传的文件"要靠**按文件名去磁盘上找**兜底（本机交付环境的原件就在项目根目录），
  这条兜底一旦宽松（模糊匹配/取第一个命中），用户下载到的就可能是**另一个文件**，
  而这种错误当场看不出来。

所以下面把"该找到的能找到""不该找到的绝不猜"两类都钉死。
"""
import os

import pytest

from config import settings
from services import file_store

PDF_BYTES = b"%PDF-1.4 fake"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把留存目录与兜底目录都换到临时目录：测试**绝不碰**真实项目根目录。"""
    store_dir = tmp_path / "file_store"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    monkeypatch.setattr(settings, "file_store_dir", str(store_dir))
    monkeypatch.setattr(settings, "file_legacy_search_dirs", str(legacy_dir))
    return store_dir, legacy_dir


class TestSaveAndLocate:
    def test_saved_original_is_found_by_uuid(self, store):
        store_dir, _ = store
        path = file_store.save_original("uuid-1", "手册.pdf", PDF_BYTES)

        assert os.path.isfile(path), "留存目录不存在时也要自动创建"
        assert os.path.dirname(path) == str(store_dir)
        with open(path, "rb") as fh:
            assert fh.read() == PDF_BYTES, "留存件必须与原文件逐字节一致"
        assert file_store.locate("uuid-1", "手册.pdf") == path

    def test_stored_name_uses_uuid_and_keeps_extension(self, store):
        store_dir, _ = store
        path = file_store.save_original("uuid-2", "我的简历 (AI应用开发)(3).pdf", PDF_BYTES)

        # 用 uuid 命名：同名文件不会互相覆盖（原名只留在 DB 里做展示）
        assert os.path.basename(path) == "uuid-2.pdf"
        # 不留 .part 残留：下载不该看到"写了一半"的文件
        assert not os.path.exists(f"{path}.part")

    def test_same_name_twice_does_not_overwrite(self, store):
        """同名不同 uuid（不同库/不同版本）必须各存一份。"""
        first = file_store.save_original("uuid-a", "同名.pdf", b"AAA")
        second = file_store.save_original("uuid-b", "同名.pdf", b"BBB")

        assert first != second
        assert open(first, "rb").read() == b"AAA"
        assert open(second, "rb").read() == b"BBB"

    def test_size_of_reports_original_size(self, store):
        file_store.save_original("uuid-3", "a.pdf", PDF_BYTES)

        assert file_store.size_of("uuid-3", "a.pdf") == len(PDF_BYTES)

    def test_locates_file_without_extension_in_name(self, store):
        """没有扩展名的上传（``unnamed``）也要能存能取。"""
        path = file_store.save_original("uuid-4", "unnamed", b"x")

        assert os.path.basename(path) == "uuid-4"
        assert file_store.locate("uuid-4", "unnamed") == path

    def test_size_of_reports_original_size(self, store):
        """``size_of`` 是面板"一次拿到可下载性 + 大小"的依据（None = 原件不在）。"""
        file_store.save_original("uuid-3", "a.pdf", PDF_BYTES)

        assert file_store.size_of("uuid-3", "a.pdf") == len(PDF_BYTES)


class TestRemoveOriginal:
    """入库失败 / 删除文件时要能清掉留存件——但**只清本项目留存件**。"""

    def test_removes_the_stored_copy(self, store):
        path = file_store.save_original("uuid-6", "a.pdf", PDF_BYTES)

        assert file_store.remove_original("uuid-6", "a.pdf") is True
        assert not os.path.exists(path)
        assert file_store.locate("uuid-6", "a.pdf") is None

    def test_returns_false_when_there_is_nothing_to_remove(self, store):
        assert file_store.remove_original("uuid-none", "a.pdf") is False

    def test_never_touches_legacy_originals(self, store):
        """兜底目录里的是**用户自己的文件**（项目根目录放的就是交付资料），绝不删。"""
        _, legacy_dir = store
        original = legacy_dir / "a.pdf"
        original.write_bytes(b"LEGACY")

        assert file_store.remove_original("uuid-7", "a.pdf") is False
        assert original.exists()

    def test_illegal_uuid_is_rejected(self, store):
        assert file_store.remove_original("../escape", "a.pdf") is False


class TestLegacyFallback:
    """改造前入库的文件没有留存件，只能按文件名去兜底目录找（3 个公共库文档靠它）。"""

    def test_finds_by_exact_filename_in_legacy_dir(self, store):
        _, legacy_dir = store
        original = legacy_dir / "SAGE AI 智能问答助手.pdf"
        original.write_bytes(PDF_BYTES)

        assert file_store.locate("no-such-uuid", "SAGE AI 智能问答助手.pdf") == str(original)
        assert file_store.size_of("no-such-uuid", "SAGE AI 智能问答助手.pdf") == len(PDF_BYTES)

    def test_stored_copy_wins_over_legacy(self, store):
        """两者都在时必须用**本项目留存件**：它才是这次上传的那份。"""
        _, legacy_dir = store
        (legacy_dir / "a.pdf").write_bytes(b"LEGACY")
        stored = file_store.save_original("uuid-5", "a.pdf", b"STORED")

        assert file_store.locate("uuid-5", "a.pdf") == stored

    def test_missing_file_returns_none(self, store):
        assert file_store.locate("uuid-x", "根本不存在.pdf") is None
        assert file_store.size_of("uuid-x", "根本不存在.pdf") is None

    def test_similar_name_is_not_a_match(self, store):
        """只认**精确**文件名：不能用"包含/前缀"这类模糊匹配，否则会下到别的文件。"""
        _, legacy_dir = store
        (legacy_dir / "手册2026.pdf").write_bytes(PDF_BYTES)

        assert file_store.locate("uuid-y", "手册.pdf") is None

    def test_ambiguous_match_returns_none(self, tmp_path, monkeypatch):
        """两个兜底目录里都有同名文件 → 返回 None（宁可 404，也别让用户下到别人的那份）。"""
        first, second = tmp_path / "d1", tmp_path / "d2"
        first.mkdir(), second.mkdir()
        for d in (first, second):
            (d / "同名.pdf").write_bytes(PDF_BYTES)
        monkeypatch.setattr(settings, "file_store_dir", str(tmp_path / "store"))
        monkeypatch.setattr(settings, "file_legacy_search_dirs", f"{first},{second}")

        assert file_store.locate("uuid-z", "同名.pdf") is None

    def test_legacy_dirs_defaults_to_project_root(self, monkeypatch):
        """留空 = 项目根目录（交付环境的原件就放在那里），不能退化成"当前工作目录"。"""
        monkeypatch.setattr(settings, "file_legacy_search_dirs", "")

        assert file_store.legacy_dirs() == [settings.base_dir]


class TestPathSafety:
    """路径穿越：``filename`` 现在来自 DB，但这一层不能被"上游一定干净"绑架。"""

    @pytest.mark.parametrize(
        "evil",
        [
            "../outside.pdf",
            "..\\outside.pdf",
            "sub/dir.pdf",
            "C:\\Windows\\win.ini",
            "..",
            "",
            "a\x00b.pdf",
        ],
    )
    def test_directory_traversal_names_are_never_used_as_paths(self, store, evil):
        _, legacy_dir = store
        (legacy_dir / "outside.pdf").write_bytes(PDF_BYTES)   # 诱惑命中

        assert file_store.locate("uuid-evil", evil) is None

    def test_illegal_file_uuid_is_rejected_on_save(self, store):
        with pytest.raises(ValueError):
            file_store.save_original("../escape", "a.pdf", PDF_BYTES)

    def test_ext_is_lowercased_and_bounded(self, store):
        assert file_store.stored_path("u", "A.PDF").endswith("u.pdf")
        # 一长串"点"不该把留存件名撑爆（超过 12 字符的"扩展名"直接丢掉）
        assert file_store.stored_path("u", "x.abcdefghijklmnop").endswith("u")
