"""黄金测试集加载器。

刻意不 import ``config``：这样 ``tests/unit/test_golden_set.py`` 这类
纯离线校验可以在没配数据库密码/API Key 的环境里直接跑。

支持两种评测集格式：

* ``.py``  —— 模块内的 ``GOLDEN_SET`` 列表（**默认**，见 golden_set.py 顶部的说明）
* ``.json`` —— ``{"items": [...]}`` 或纯数组（方便自定义/外部评测集）
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os

logger = logging.getLogger("rag.eval.loader")

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认黄金测试集：Python 数据模块
DEFAULT_GOLDEN_SET = os.path.join(_EVAL_DIR, "golden_set.py")


def load_golden_set(path: str | None = None) -> list[dict]:
    """读取并校验黄金测试集，返回规范化后的列表。

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 格式不对，或没有任何有效条目
    """
    path = path or DEFAULT_GOLDEN_SET
    if not os.path.exists(path):
        raise FileNotFoundError(f"没有找到黄金测试集：{path}")

    raw = _read_raw(path)
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not items:
        raise ValueError(f"黄金测试集为空或格式不正确：{path}")

    cleaned = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            logger.warning("ragas_golden_item_skipped", extra={"index": idx, "reason": "not a dict"})
            continue
        question = (item.get("question") or "").strip()
        ground_truth = (item.get("ground_truth") or "").strip()
        if not question or not ground_truth:
            logger.warning("ragas_golden_item_skipped", extra={"index": idx, "reason": "missing field"})
            continue
        cleaned.append(
            {
                "id": item.get("id") or f"item-{idx}",
                "question": question,
                "ground_truth": ground_truth,
                "tags": item.get("tags") or [],
            }
        )

    if not cleaned:
        raise ValueError(f"黄金测试集里没有任何有效条目（question / ground_truth 都必填）：{path}")
    return cleaned


def _read_raw(path: str):
    lower = path.lower()
    if lower.endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    if lower.endswith(".py"):
        return _load_python_module(path)
    raise ValueError(f"不支持的评测集格式（只认 .py / .json）：{path}")


def _load_python_module(path: str):
    """从 .py 模块里取出 GOLDEN_SET，不走 sys.path，避免污染全局导入表。"""
    spec = importlib.util.spec_from_file_location("rag_golden_set_module", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载评测集模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    data = getattr(module, "GOLDEN_SET", None)
    if not data:
        raise ValueError(f"{path} 里没有定义 GOLDEN_SET")
    return data
