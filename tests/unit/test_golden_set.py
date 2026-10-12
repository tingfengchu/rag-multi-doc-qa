"""黄金测试集格式校验（不依赖任何外部服务，默认就参与 pytest）。

这里直接 import ``evaluation.loader``：它刻意不 import config，
所以本文件在没配数据库密码 / API Key 的干净环境里也能跑。

运行：.venv\\Scripts\\python.exe -m pytest tests/unit -v
"""
import os
import re

import pytest

from evaluation.loader import DEFAULT_GOLDEN_SET, load_golden_set


@pytest.fixture(scope="module")
def golden_set():
    return load_golden_set()


def test_default_golden_set_exists():
    assert os.path.exists(DEFAULT_GOLDEN_SET), f"缺少默认评测集：{DEFAULT_GOLDEN_SET}"
    assert DEFAULT_GOLDEN_SET.endswith(".py"), (
        "默认评测集必须是 .py 数据模块：开发机上的透明加密会把 .json 写成"
        "Python 读不了的密文（见 evaluation/golden_set.py 顶部说明）"
    )


def test_golden_set_is_a_nonempty_list(golden_set):
    assert isinstance(golden_set, list)
    assert len(golden_set) >= 10, "评测集太小，指标没有统计意义"


def test_ids_are_present_and_unique(golden_set):
    ids = [item["id"] for item in golden_set]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"存在重复的 id：{duplicates}"


def test_every_item_has_question_and_ground_truth(golden_set):
    for item in golden_set:
        assert item["question"].strip(), f"id={item['id']} 缺少 question"
        assert item["ground_truth"].strip(), f"id={item['id']} 缺少 ground_truth"


def test_question_looks_like_a_question(golden_set):
    """问题要真的是"提问"（防止把陈述句误填进来）。"""
    for item in golden_set:
        question = item["question"]
        assert question.endswith(("？", "?")), f"id={item['id']} 的问题不像提问：{question!r}"


def test_ground_truth_is_substantive(golden_set):
    """标准答案不能敷衍，否则 recall 类指标没有区分度。"""
    for item in golden_set:
        assert len(item["ground_truth"]) >= 15, (
            f"id={item['id']} 的 ground_truth 太短：{item['ground_truth']!r}"
        )


def test_tags_are_lists_of_strings(golden_set):
    for item in golden_set:
        tags = item["tags"]
        assert isinstance(tags, list), f"id={item['id']} 的 tags 必须是数组"
        assert all(isinstance(t, str) and t for t in tags), f"id={item['id']} 的 tags 含非法元素"


def test_questions_have_no_duplicates(golden_set):
    questions = [item["question"] for item in golden_set]
    assert len(questions) == len(set(questions)), "存在重复的问题"


def test_ids_are_cli_friendly(golden_set):
    """id 会出现在日志与报告字段里，限制成安全字符。"""
    pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    for item in golden_set:
        assert pattern.match(item["id"]), f"id 含不安全字符：{item['id']!r}"


def test_covers_multiple_topics(golden_set):
    """评测集要覆盖多份文档，否则测不出跨文档检索能力。"""
    all_tags = {tag for item in golden_set for tag in item["tags"]}
    assert len(all_tags) >= 6, f"话题覆盖太少，只有：{sorted(all_tags)}"


def test_external_json_path_is_supported(tmp_path):
    """.json 评测集（自定义场景）也要能用。"""
    json_path = tmp_path / "custom_golden_set.json"
    json_path.write_text(
        '{"items": [{"id": "x", "question": "问题？", "ground_truth": "这是一段足够长的标准答案。"}]}',
        encoding="utf-8",
    )
    items = load_golden_set(str(json_path))
    assert len(items) == 1
    assert items[0]["id"] == "x"


def test_missing_field_entries_are_dropped(tmp_path):
    """缺字段的条目应被静默丢弃，而不是让整轮评测崩掉。"""
    json_path = tmp_path / "bad_golden_set.json"
    json_path.write_text(
        '{"items": ['
        '{"id": "ok", "question": "问题？", "ground_truth": "这是一段足够长的标准答案。"},'
        '{"id": "bad", "question": "只有问题没有答案？"}'
        ']}',
        encoding="utf-8",
    )
    items = load_golden_set(str(json_path))
    assert [item["id"] for item in items] == ["ok"]


def test_all_empty_entries_raise(tmp_path):
    json_path = tmp_path / "empty_golden_set.json"
    json_path.write_text('{"items": [{"id": "bad", "question": "问题？"}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_golden_set(str(json_path))


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_golden_set("no/such/golden_set.py")



def test_golden_set_is_a_nonempty_array(golden_set):
    assert isinstance(golden_set, list)
    assert len(golden_set) >= 10, "评测集太小，指标没有统计意义"


def test_ids_are_present_and_unique(golden_set):
    ids = [item.get("id") for item in golden_set]
    assert all(ids), "每道题都必须有 id"
    assert len(ids) == len(set(ids)), f"存在重复的 id：{sorted({i for i in ids if ids.count(i) > 1})}"


def test_every_item_has_question_and_ground_truth(golden_set):
    for item in golden_set:
        question = (item.get("question") or "").strip()
        ground_truth = (item.get("ground_truth") or "").strip()
        assert question, f"id={item.get('id')} 缺少 question"
        assert ground_truth, f"id={item.get('id')} 缺少 ground_truth"


def test_ground_truth_is_substantive(golden_set):
    """标准答案不能是敷衍的一句话，否则 recall 类指标没有区分度。"""
    for item in golden_set:
        ground_truth = item["ground_truth"].strip()
        assert len(ground_truth) >= 15, f"id={item['id']} 的 ground_truth 太短：{ground_truth!r}"


def test_tags_are_lists_of_strings(golden_set):
    for item in golden_set:
        tags = item.get("tags", [])
        assert isinstance(tags, list), f"id={item.get('id')} 的 tags 必须是数组"
        assert all(isinstance(t, str) and t for t in tags), f"id={item.get('id')} 的 tags 含非法元素"


def test_questions_have_no_duplicates(golden_set):
    questions = [item["question"] for item in golden_set]
    assert len(questions) == len(set(questions)), "存在重复的问题"


def test_ids_are_cli_friendly(golden_set):
    """id 会被用作文件名/日志字段，限制成安全字符。"""
    import re

    pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    for item in golden_set:
        assert pattern.match(item["id"]), f"id 含不安全字符：{item['id']!r}"
