"""精排（Cross-Encoder）打分的通用工具。

为什么单独抽成一个模块？
    * ``services/retrieve_service.py`` 顶部 import 了 streamlit / langchain，
      纯逻辑写在那里就没法写离线单测（``tests/unit`` 刻意不依赖 config / 外部服务）；
    * 这里承载了**实测出来的两条关键防护**，需要被单测直接锁住：
      ① 归一化必须按模型族选映射；② 精排没有区分度时必须退回 RRF 顺序。

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

---------------- 归一化：按模型族选映射（2026-10-13 实测） ----------------

两个模型都用 ``HuggingFaceCrossEncoder.score()`` 直接跑过（结果见
``test_rerank.py`` 里钉死的数值），**同一个 sigmoid 走天下两个方向都会坏**：

======================  ==============================  ============================
模型                    ``.score()`` 实际返回           归一化
======================  ==============================  ============================
MS MARCO MiniLM（英文）  原始 logits，实测 -2.0 ~ +7.2    ``sigmoid(x / LOGIT_TEMPERATURE)``
BAAI/bge-reranker（中文） **已经是概率** 0.00004 ~ 0.9993  裁剪到 [0, 0.999]（``clip_unit_interval`` + ``MAX_RERANK_SCORE``）
======================  ==============================  ============================

* 对 logits 用 T=1 的 sigmoid：实测 5 个中文 pair 得到
  0.9394 / 0.9974 / 0.1221 / 0.8780 / 0.9992 —— **不相关块(0.9974) 反而高于相关块(0.9394)**，
  且最高的两块贴着 1.00，前端显示的"置信度 1.00"就是这么来的；
* 对概率再套 sigmoid 是**二次压缩**：0.9993 → 0.7309、0.000037 → 0.500009。
  后者尤其致命：``api/routers/chat.py`` 的 ``CONFIDENCE_THRESHOLD = 0.5`` 会被它骗过
  —— 明显无关的块算出 0.500009 ≥ 0.5，于是"照常回答"而不是"未找到足够相关信息"。

温度取 5 的依据：logits 实测落在 [-15, +15]，除以 5 后进入 sigmoid 的线性区，
3 → 0.646、8 → 0.832（差 0.19，肉眼可辨、也远不到 1.00），且强相关时不饱和
（raw 20 → 0.982、raw 40 → 0.9996）。
刻意**不用 min-max**：它把每批候选强行拉成 0~1（每批必有一个 1.000），置信度就失去
跨查询可比性，``CONFIDENCE_THRESHOLD`` 随之失去意义；也不用 ``x/10`` 裁剪：
raw>10 时又会重新压成 1.00 —— 正是本次要修的那个 bug。

注意两条判据的分工（别顺手合并）：
* **展示/门控阈值**用带温度的映射（新旧映射在 raw=0 处都恰好等于 0.5，所以
  ``CONFIDENCE_THRESHOLD=0.5`` 的语义不变）；
* **饱和探测**（``is_informative``）固定用 T=1 的快照：T=1 在 |x|>6 时把差值压成 0，
  是**最灵敏**的饱和探测器。实测的真实候选集在 T=1 与 T=5 下判决一致（都判饱和），
  但换成极端 logits 就未必——门控宁可误判"不可信"（退回 RRF），也不能把噪声当信号。
"""
from __future__ import annotations

import math

#: 归一化分数（sigmoid 之后，取值 0~1）的极差下限。
#: 低于它就认为本次精排没有区分能力，按"不可信"处理、保留 RRF 顺序。
#: 取值依据：实测英文模型跑中文的极差 ≈0.0007；而正常可用的精排
#: （英文查询 + MiniLM，或中文查询 + bge-reranker）极差通常在 0.1 以上，
#: 0.02 这个阈值离两侧都有 1 个数量级的安全边际。
RERANK_MIN_SPREAD = 0.02

#: 默认重排模型（**唯一真源**：``api/schemas.py`` 与前端 ``client.ts`` 都对齐它）。
#: 选 BGE 而不是 MiniLM 的理由见模块 docstring：MiniLM 是英文模型，中文上实测无区分度。
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"

#: 展示用温度：把实测落在 [-15, +15] 的 logits 压到 0~1 且保留可读区分度。
LOGIT_TEMPERATURE = 5.0

#: 门控用温度：不缩放 = 最灵敏的饱和探测器（见模块 docstring 末段）。
RERANK_GATE_TEMPERATURE = 1.0

#: 已实测"返回原始 logits"的模型（必须缩放后再 sigmoid）。
LOGIT_MODEL_MARKERS = ("ms-marco",)

#: 已实测"直接返回 [0,1] 概率"的模型族（再套 sigmoid 就是二次压缩）。
PROBABILITY_MODEL_MARKERS = ("bge",)

#: 展示上限：真实打分的概率**永远不许等于 1.00**，给 1.00 留一个极小余量。
#:
#: 为什么要留（2026-10-13 用户复验"置信度还是 1.00"）：BGE 的输出理论上在 [0,1]，
#: 实测也贴到过 0.9993，而**极端样本会刚好落在 1.000000**。而 1.00 在本系统里另有
#: 精确含义：它是"这次没做分数门控"的**哨兵值**——
#: ``retrieve_service.rerank_documents(enable_rerank=False)`` 与
#: ``api/routers/chat.py:DEFAULT_RERANK_SCORE`` 都写 1.0。两者混在一起，用户就再也
#: 分不清"完美匹配"和"根本没打分"，这正是要消掉的歧义（前端 ``formatConfidence``
#: 与后端 ``_format_confidence`` 同口径）。
#:
#: ⚠️ 只裁**概率分支**：logits 分支走 sigmoid，在 (0,1) 开区间内渐近；若给 sigmoid
#: 的结果也封顶，``is_informative``（与展示共用 ``normalize_scores``）会把极端 logits
#: 11/10/9 一起压成 0.999 → 极差 0 → 反而**判不出**饱和（正是闸门失效的那类 bug）。
MAX_RERANK_SCORE = 0.999


def sigmoid(x: float) -> float:
    """把 Cross-Encoder 的原始 logits 压到 0~1（溢出时按方向取端点）。"""
    try:
        return 1 / (1 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def clip_unit_interval(value: float) -> float:
    """裁剪到 [0,1]（给"已经返回概率"的模型用，如 BGE 系列）。"""
    return max(0.0, min(1.0, float(value)))


def is_probability_model(model_name: str | None) -> bool:
    """该模型的 ``.score()`` 是否**已经**返回 [0,1] 的概率（实测见模块 docstring）。

    ``LOGIT_MODEL_MARKERS`` 优先判断：两表当前不会同时命中，但先把"实测返回 logits"
    的模型认出来，能避免将来某个模型名同时命中两表时被误裁剪——裁剪对 logits 是
    **不可逆**的信息丢失（raw 8 与 raw 12 都会变成 1.0，正是本次要修的 bug）。
    """
    if not model_name:
        return False
    name = str(model_name).lower()
    if any(marker in name for marker in LOGIT_MODEL_MARKERS):
        return False
    return any(marker in name for marker in PROBABILITY_MODEL_MARKERS)


def normalize_scores(raw_scores, model_name: str | None = None,
                     temperature: float = LOGIT_TEMPERATURE) -> list:
    """把一批原始分数压到 0~1（**保持原有顺序**）——按模型族选映射。

    * 概率模型（BGE 系列，实测直接返回概率）→ 裁剪到 ``[0, MAX_RERANK_SCORE]``
      （上界不是 1.0：1.00 留给"这次没做分数门控"的哨兵，见 ``MAX_RERANK_SCORE``）；
    * logits 模型（MiniLM 等）→ ``sigmoid(x / temperature)``。

    ``model_name`` 省略时按 logits 处理（历史行为，调用方应尽力传模型名）；
    ``temperature`` 只在 logits 分支生效——展示用 ``LOGIT_TEMPERATURE``，
    饱和探测用 ``RERANK_GATE_TEMPERATURE``（两者分工见模块 docstring）。

    ⚠️ 新增模型必须先实测它返回 logits 还是概率，再加进对应的标记表；
    判错的方向都能被单测抓到（``test_rerank.py::TestModelAwareNormalization``）。
    """
    numbers = [float(s) for s in raw_scores]
    if is_probability_model(model_name):
        return [min(clip_unit_interval(value), MAX_RERANK_SCORE) for value in numbers]
    if not temperature or temperature <= 0:
        raise ValueError("temperature 必须为正数：除以 0 会把所有 logits 直接压成端点")
    return [sigmoid(value / temperature) for value in numbers]


def spread_of(values) -> float:
    """极差（max - min）。空列表返回 0.0（没有任何信息）。"""
    numbers = [float(v) for v in values]
    if not numbers:
        return 0.0
    return max(numbers) - min(numbers)


def is_informative(raw_scores, min_spread: float = RERANK_MIN_SPREAD, window: int | None = None,
                   model_name: str | None = None):
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

    ``model_name``：按模型族选映射（概率模型裁剪、logits 模型 sigmoid，见
    ``normalize_scores``）。**温度固定 ``RERANK_GATE_TEMPERATURE``（=1，不缩放）**：
    本函数要的是"最灵敏的饱和探测"，不是可读性——实测同一批真实候选在 T=1 与 T=5
    下判决一致（都判饱和），但极端 logits（如 11/10/9）在 T=5 下会被误判成"有区分度"，
    于是随机顺序又覆盖回 RRF 顺序。门控宁可误判为不可信。
    """

    numbers = [float(s) for s in raw_scores]
    if len(numbers) < 2:
        return True, 0.0

    normalized = sorted(
        normalize_scores(numbers, model_name=model_name, temperature=RERANK_GATE_TEMPERATURE),
        reverse=True,
    )
    if window is not None and window > 0:
        normalized = normalized[:window]
    if len(normalized) < 2:
        return True, 0.0

    spread = spread_of(normalized)
    return spread >= min_spread, spread
