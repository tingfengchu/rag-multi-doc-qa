"""RAGAS 端到端评测的集成测试。

默认**不会跑**：``pytest.ini`` 的 addopts 里带了 ``-m "not ragas"``，
因为这一组测试会真实调用 LLM API（花 token、依赖网络），
并且需要 MySQL / ES / Redis / Chroma 全部就绪、目标 scope 里已经导入了文档。

手动运行：

    .venv\\Scripts\\python.exe -m pytest -m ragas -v -s
    .venv\\Scripts\\python.exe -m pytest tests/integration/test_ragas_eval.py -m ragas -v
"""
import pytest

from services import eval_service

pytestmark = [pytest.mark.integration, pytest.mark.ragas]

# 质量门禁：低于这些值说明检索/生成链路退化，需要先修再上线
MIN_FAITHFULNESS = 0.60
MIN_ANSWER_RELEVANCY = 0.50


def _skip_if_no_dependency():
    if not eval_service.ragas_available():
        pytest.skip("未安装评测依赖，跳过（见 requirements-eval.txt，建议装在 .venv-eval）")


def _skip_if_empty_scope(scope: str = "public"):
    from services import vector_service

    vectorstore = vector_service.get_vectorstore(scope)
    if vector_service.count(vectorstore) == 0:
        pytest.skip(f"{scope} 向量库为空，先上传文档再跑评测")
    return vectorstore


def test_golden_set_has_enough_items():
    items = eval_service.load_golden_set()
    assert len(items) >= 10
    assert all(item["question"] and item["ground_truth"] for item in items)


def test_pipeline_returns_contexts_and_answers():
    """先验证检索+生成这一半，避免 RAGAS 报错时不知道是数据问题还是依赖问题。"""
    _skip_if_empty_scope()
    rows = eval_service.collect_predictions("public", limit=2)
    assert len(rows) == 2
    for row in rows:
        assert row["answer"].strip(), f"题目「{row['question']}」没有生成答案"
        assert not row["answer"].startswith("【链路异常】"), row["answer"]
        assert row["contexts"], f"题目「{row['question']}」没有检索到任何上下文"


def test_ragas_scores_meet_quality_gate():
    """真实调 RAGAS 打分，并卡住忠实度 / 相关性门禁。"""
    _skip_if_no_dependency()
    _skip_if_empty_scope()

    report = eval_service.run_ragas_evaluation(scope="public", limit=5, write_report=False)
    scores = report["scores"]

    assert scores, "RAGAS 没有返回任何指标（检查裁判模型与 API Key）"
    assert scores.get("faithfulness", 0) >= MIN_FAITHFULNESS, f"忠实度不达标：{scores}"
    assert scores.get("answer_relevancy", 0) >= MIN_ANSWER_RELEVANCY, f"相关性不达标：{scores}"
