"""精排（Cross-Encoder）打分的通用工具。

为什么单独抽成一个模块？
    * ``services/retrieve_service.py`` 顶部 import 了 streamlit / langchain，
      纯逻辑写在那里就没法写离线单测（``tests/unit`` 刻意不依赖 config / 外部服务）；
    * 这里承载了一条**实测出来的关键防护**，需要被单测直接锁住。

背景（2026-10 审计实测，Q1「张嘉亮的手机号和邮箱分别是多少？」）：
    ``cross-encoder/ms-marco-MiniLM-L-6-v2`` 是**英文** MS MARCO 模型。
    拿它给中文问句打分时，分数经 sigmoid 后全部堆在 0.999x 且几乎没有差别
    （实测 0.999823 / 0.999177 / 0.999146，极差仅 **0.0007**），
    于是"精排"退化成随机打乱：BM25 已经排第 1（score 16.75）、RRF 融合后
    仍然是第 1 的简历联系方式块，被挤出 top-3，最终
    ``context_recall=0``，模型只能回答"文档中未提供该信息，无法回答"。

结论（本模块的契约）：
    **精排只有在确实有区分度时才允许覆盖 RRF 顺序**；一旦分数饱和
    （极差低于 ``RERANK_MIN_SPREAD``），说明该模型对本次查询没有判断力，
    必须回退到 RRF 顺序，绝不静默让"随机顺序"覆盖已经正确的排序。
"""
from __future__ import annotations

import math

#: 归一化分数（sigmoid 之后，取值 0~1）的极差下限。
#: 低于它就认为本次精排没有区分能力，按"不可信"处理、保留 RRF 顺序。
#: 取值依据：实测英文模型跑中文的极差 ≈0.0007；而正常可用的精排
#: （英文查询 + MiniLM，或中文查询 + bge-reranker）极差通常在 0.1 以上，
#: 0.02 这个阈值离两侧都有 1 个数量级的安全边际。
RERANK_MIN_SPREAD = 0.02


def sigmoid(x: float) -> float:
    """把 Cross-Encoder 的原始 logits 压到 0~1（溢出时按方向取端点）。"""
    try:
        return 1 / (1 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def normalize_scores(raw_scores) -> list:
    """把一批原始分数批量 sigmoid 归一化（保持原有顺序）。"""
    return [sigmoid(float(s)) for s in raw_scores]


def spread_of(values) -> float:
    """极差（max - min）。空列表返回 0.0（没有任何信息）。"""
    numbers = [float(v) for v in values]
    if not numbers:
        return 0.0
    return max(numbers) - min(numbers)


def is_informative(raw_scores, min_spread: float = RERANK_MIN_SPREAD, window: int | None = None):
    """判断这批精排分数**有没有区分度**，能不能拿来重排。

    返回 ``(可信吗, 归一化后的极差)``：

    * 空输入          -> ``(True, 0.0)``：没有候选，调用方自会按原顺序取；
    * 只有 1 个候选    -> ``(True, 0.0)``：没有"排序"可言，不构成"乱排"风险；
    * 窗口内极差 < min_spread -> ``(False, 极差)``：分数饱和，典型的"英文模型跑中文语料"，
      此时必须回退 RRF 顺序。

    ``window``（重要）：只看"最终会被输出的前 N 条"。原因是实测出来的：
    top-10 里的**整体**极差往往很大（相关文档 8.5 vs 明显无关文档 -2），
    但**前 3 名之间** sigmoid 后只差 0.0007——也就是说"到底哪 3 条被选中"
    完全是噪声。判定必须落在"决定输出的那段窗口"上，用全局极差会漏判。
    """

    numbers = [float(s) for s in raw_scores]
    if len(numbers) < 2:
        return True, 0.0

    normalized = sorted(normalize_scores(numbers), reverse=True)
    if window is not None and window > 0:
        normalized = normalized[:window]
    if len(normalized) < 2:
        return True, 0.0

    spread = spread_of(normalized)
    return spread >= min_spread, spread
