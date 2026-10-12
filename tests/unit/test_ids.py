"""``utils.ids`` 的单元测试（纯函数，无需任何外部服务，可离线运行）。

这里锁定的是一组**契约**，而不是实现细节：

1. ``new_uuid()`` 必须返回符合 UUID4 语法的字符串，且**绝不包含双下划线**；
2. ``build_source()`` 必须产出 ``owner__file__filename`` 三段式，
   保证 ``source.split("__")`` 永远只能切出 3 段（否则前端还原文件名会错位）；
3. 同名文件由 UUID 保证唯一性，重复上传也不会互相覆盖。

运行：``.venv\\Scripts\\python.exe -m pytest tests/unit -v``
"""
import uuid

import pytest

from utils.ids import build_source, new_uuid


class TestNewUuid:
    def test_returns_string(self):
        assert isinstance(new_uuid(), str)

    def test_is_valid_uuid4(self):
        value = new_uuid()
        parsed = uuid.UUID(value)
        assert parsed.version == 4
        # 必须是标准小写连字符格式，否则拼进 source 后前端解析会不一致
        assert str(parsed) == value

    def test_is_unique(self):
        assert len({new_uuid() for _ in range(2000)}) == 2000

    def test_contains_no_double_underscore(self):
        # 关键契约：UUID 里不能出现 "__"，否则 source 的三段式就废了
        for _ in range(500):
            assert "__" not in new_uuid()


class TestBuildSource:
    def test_basic_format(self):
        owner, file_id = new_uuid(), new_uuid()
        assert build_source(owner, file_id, "resume.pdf") == f"{owner}__{file_id}__resume.pdf"

    def test_split_recovers_original_filename(self):
        owner, file_id = new_uuid(), new_uuid()
        parts = build_source(owner, file_id, "2024 年度报告.pdf").split("__")
        assert parts == [owner, file_id, "2024 年度报告.pdf"]

    def test_double_underscore_in_filename_is_sanitized(self):
        # 文件名自带的 "__" 必须被压成 "_"，否则会多切出一段
        parts = build_source("o", "f", "a__b__c.pdf").split("__")
        assert parts == ["o", "f", "a_b_c.pdf"]

    def test_same_filename_different_uuid_is_unique(self):
        assert build_source(new_uuid(), new_uuid(), "same.pdf") != build_source(
            new_uuid(), new_uuid(), "same.pdf"
        )

    def test_same_inputs_are_deterministic(self):
        assert build_source("owner-1", "file-1", "x.pdf") == build_source("owner-1", "file-1", "x.pdf")

    def test_special_characters_are_preserved(self):
        source = build_source("o", "f", "简历（最终版） #2.pdf")
        assert source.endswith("简历（最终版） #2.pdf")

    def test_empty_filename_is_tolerated(self):
        assert build_source("o", "f", "") == "o__f__"

    @pytest.mark.parametrize(
        "filename",
        [
            "a.pdf",
            "有__分隔符.pdf",
            "空格 名字.docx",
            "mixed.pdf_name.xlsx",
            "single_underscore.pptx",
            "____.pdf",
        ],
    )
    def test_split_always_yields_three_parts(self, filename):
        source = build_source(new_uuid(), new_uuid(), filename)
        assert len(source.split("__")) == 3

