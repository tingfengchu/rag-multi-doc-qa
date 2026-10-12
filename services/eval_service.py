"""RAGAS 自动评测服务。

把「黄金测试集 → 真实检索 + 真实生成 → RAGAS 打分」这条链路封装成可复用函数，
供两处调用：

* ``scripts/evaluate_ragas.py``（命令行：本地评测 / CI 卡质量门禁）
* ``app.py`` 管理员面板（面试演示时现场跑一遍）

为什么单独抽一层？
    RAGAS 的依赖里带着 datasets / openai / langchain-core 一大坨，
    而且不同大版本 API 差异明显。把它隔离在 ``eval_service`` 里并**延迟导入**，
    这样没装 RAGAS 的环境照样能跑主站，只在点「开始评测」时提示缺依赖。

四项指标（全部 0~1，越高越好）：
    faithfulness        答案是否完全由检索到的上下文支撑（不编造）
    answer_relevancy    答案是否切题
    context_precision   检索到的上下文里「有用」的部分排得够不够靠前
    context_recall      标准答案所需的信息有没有被全都捞回来
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os

from config import settings
from evaluation.loader import DEFAULT_GOLDEN_SET, load_golden_set  # noqa: F401  对外再导出

logger = logging.getLogger("rag.eval")

DEFAULT_OUTPUT_DIR = os.path.join(settings.base_dir, "evaluation_output")

# 指标中文名，便于在 UI / 报告里展示
METRIC_LABELS = {
    "faithfulness": "忠实度（不编造）",
    "answer_relevancy": "答案相关性",
    "context_precision": "上下文精确率",
    "context_recall": "上下文召回率",
}


# --------------------------------------------------------------------------
# 依赖探测
# --------------------------------------------------------------------------
def ragas_available() -> bool:
    """RAGAS 是否可用（不抛异常，供 UI 做友好提示）。"""
    try:
        import datasets  # noqa: F401
        import ragas  # noqa: F401
    except Exception:
        return False
    return True


def ragas_version() -> str:
    try:
        import ragas

        return getattr(ragas, "__version__", "unknown")
    except Exception:
        return "not-installed"


# --------------------------------------------------------------------------
# 黄金测试集：实现已下沉到 evaluation/loader.py
# （这样单测校验评测集时不需要 import config / 配好密钥）
# eval_service.load_golden_set 是从 loader 再导出过来的，脚本照旧可用。
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 跑一遍真实链路，收集 (问题, 答案, 上下文, 标准答案)
# --------------------------------------------------------------------------
def collect_predictions(scope: str, limit: int | None = None, k: int = 10,
                        golden_set_path: str | None = None) -> list[dict]:
    """对每条问题跑真实的混合检索 + 生成，返回待打分的样本。

    刻意复用线上同一套 ``retrieve_service`` / ``rag_service``，
    保证评测的就是用户实际会体验到的链路（而不是另写一个简化版来刷分）。
    """
    # 延迟导入：避免 import eval_service 就把 torch / docling 全拉起来
    from services import rag_service, retrieve_service, vector_service

    items = load_golden_set(golden_set_path)
    if limit:
        items = items[:limit]

    vectorstore = vector_service.get_vectorstore(scope)

    rows = []
    for i, item in enumerate(items, 1):
        question = item["question"]
        try:
            docs = retrieve_service.get_hybrid_retriever_with_es(
                query=question, scope=scope, vectorstore=vectorstore, k=k
            )
            contexts = [
                d.page_content for d in docs
                if d.page_content and d.page_content.strip()
            ]
            answer = rag_service.generate_answer(question, docs)
        except Exception as e:
            # 单题失败不影响整轮评测，但要留下痕迹
            logger.error(
                "ragas_pipeline_failed",
                extra={"question": question, "err": str(e)},
            )
            docs, contexts, answer = [], [], f"【链路异常】{e}"

        logger.info(
            "ragas_sample_done",
            extra={"index": i, "question": question[:40], "contexts": len(contexts)},
        )
        rows.append(
            {
                "id": item["id"],
                "tags": item["tags"],
                "question": question,
                "ground_truth": item["ground_truth"],
                "answer": answer,
                "contexts": contexts,
            }
        )
    return rows


# --------------------------------------------------------------------------
# RAGAS 打分
# --------------------------------------------------------------------------
def _build_metrics(evaluator_llm, evaluator_embeddings):
    """构造指标对象，并把 LLM / Embeddings 挂上去。

    ragas 各版本的指标对象属性名都是 ``llm`` / ``embeddings``；
    赋值失败也不致命（``evaluate()`` 里还有参数兜底）。
    """
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )

    metrics = [faithfulness, answer_relevancy, context_precision, context_recall]
    for metric in metrics:
        try:
            metric.llm = evaluator_llm
        except Exception:
            pass
        try:
            metric.embeddings = evaluator_embeddings
        except Exception:
            pass
    return metrics


def _build_ragas_dataset(rows: list[dict]):
    """把样本转成 ragas 能吃的数据集。

    优先用新版（0.2+）的 ``EvaluationDataset + SingleTurnSample``，
    失败则退回旧版的 ``datasets.Dataset``，跨版本都不至于直接崩。
    返回 ``(dataset, 类型名)``。
    """
    try:
        from ragas import EvaluationDataset
        from ragas.dataset_schema import SingleTurnSample

        samples = [
            SingleTurnSample(
                user_input=r["question"],
                response=r["answer"],
                retrieved_contexts=r["contexts"] or [""],
                reference=r["ground_truth"],
            )
            for r in rows
        ]
        return EvaluationDataset(samples=samples), "EvaluationDataset"
    except Exception as e:
        logger.warning("ragas_evaluation_dataset_unavailable", extra={"err": str(e)})

    from datasets import Dataset

    return (
        Dataset.from_list(
            [
                {
                    "user_input": r["question"],
                    "response": r["answer"],
                    "retrieved_contexts": r["contexts"] or [""],
                    "reference": r["ground_truth"],
                }
                for r in rows
            ]
        ),
        "datasets.Dataset",
    )


def run_ragas_evaluation(scope: str = "public", limit: int | None = None, k: int = 10,
                         golden_set_path: str | None = None,
                         write_report: bool = True) -> dict:
    """跑完整评测，返回可直接展示 / 落盘的报告 dict。"""
    if not ragas_available():
        raise RuntimeError(
            "未安装 RAGAS 依赖，请装到独立的评测环境："
            "python -m venv .venv-eval && "
            r".venv-eval\Scripts\pip install -r requirements.txt -r requirements-eval.txt"
        )

    from langchain_openai import ChatOpenAI
    from ragas import evaluate

    from services import vector_service

    # 评委模型：temperature=0 保证打分可复现
    judge_llm = ChatOpenAI(
        model="glm-4-flash",
        temperature=0,
        api_key=settings.zhipu_api_key,
        base_url=settings.openai_api_base,
    )
    judge_embeddings = vector_service.embeddings

    rows = collect_predictions(scope=scope, limit=limit, k=k, golden_set_path=golden_set_path)

    # ragas 各版本对 LLM / Embeddings 的包装类位置不同，都做兼容
    evaluator_llm = judge_llm
    try:
        from ragas.llms import LangchainLLMWrapper

        evaluator_llm = LangchainLLMWrapper(judge_llm)
    except Exception as e:
        logger.warning("ragas_llm_wrapper_unavailable", extra={"err": str(e)})

    evaluator_embeddings = judge_embeddings
    try:
        from ragas.embeddings import LangchainEmbeddingsWrapper

        evaluator_embeddings = LangchainEmbeddingsWrapper(judge_embeddings)
    except Exception as e:
        logger.warning("ragas_embeddings_wrapper_unavailable", extra={"err": str(e)})

    metrics = _build_metrics(evaluator_llm, evaluator_embeddings)
    dataset, dataset_type = _build_ragas_dataset(rows)

    # raise_exceptions=False：个别题目打分失败不会让整轮评测直接崩掉
    kwargs = {"metrics": metrics, "raise_exceptions": False}
    try:
        result = evaluate(dataset, **kwargs)
    except TypeError:
        # 老版本 evaluate() 支持显式传 llm / embeddings
        result = evaluate(
            dataset, llm=evaluator_llm, embeddings=evaluator_embeddings, **kwargs
        )

    scores, detail = _extract_scores(result)
    scores = {name: value for name, value in scores.items() if value is not None}

    report = {
        "scope": scope,
        "limit": limit,
        "k": k,
        "judge_model": "glm-4-flash",
        "embedding_model": "embedding-3",
        "ragas_version": ragas_version(),
        "dataset_type": dataset_type,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "sample_count": len(rows),
        "scores": scores,
        "metric_labels": METRIC_LABELS,
        "rows": detail,
        "report_path": None,
    }

    if write_report:
        report["report_path"] = _write_report(report)

    logger.info("ragas_evaluation_done", extra={"scope": scope, "scores": scores})
    return report


def _extract_scores(result) -> tuple[dict, list[dict]]:
    """从 ragas 的 EvaluationResult 里取出平均分与逐题明细。

    统一走 ``to_pandas()``：这是各版本里最稳定的出口，
    平均值和逐题明细可以一次拿全（比 ``dict(result)`` 稳）。
    """
    try:
        frame = result.to_pandas()
    except Exception as e:
        logger.warning("ragas_to_pandas_failed", extra={"err": str(e)})
        raw = result if isinstance(result, dict) else dict(result)
        return {key: _safe_float(value) for key, value in raw.items()}, []

    metric_names = list(METRIC_LABELS.keys())
    scores: dict = {}
    for name in metric_names:
        if name in frame.columns:
            series = frame[name].dropna()
            if len(series):
                scores[name] = float(series.mean())

    detail = []
    for _, row in frame.iterrows():
        record = {}
        for key in ("user_input", "response", "reference"):
            if key not in frame.columns:
                continue
            text = "" if row.get(key) is None else str(row.get(key))
            record[key] = text if len(text) <= 500 else text[:500] + "..."
        for name in metric_names:
            if name in frame.columns:
                record[name] = _safe_float(row.get(name))
        detail.append(record)
    return scores, detail


def _safe_float(value) -> float | None:
    """把 NaN / None / 不可转的值统一成 None，保证 JSON 可序列化。"""
    try:
        if value is None:
            return None
        number = float(value)
        if number != number:  # NaN
            return None
        return number
    except Exception:
        return None


def _write_report(report: dict) -> str:
    """把报告写到 evaluation_output/，返回路径。"""
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_scope = report["scope"].replace(":", "_")
    path = os.path.join(DEFAULT_OUTPUT_DIR, f"ragas_{safe_scope}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info("ragas_report_written", extra={"path": path})
    return path


def format_scores(scores: dict, labels: dict | None = None):
    """把分数格式化成一行行文本，命令行里直接 print。"""
    labels = labels or METRIC_LABELS
    for name, value in scores.items():
        yield f"  {labels.get(name, name):<18}{value:.4f}"


