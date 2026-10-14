"""``utils.rerank`` 的离线单测（不需要 config / ES / 模型下载）。

锁住的核心行为：精排分数饱和（没有区分度）时必须判定为"不可信"，
否则 ``retrieve_service`` 会拿随机顺序覆盖正确的 RRF 顺序，
导致简历里的联系方式块被挤出 top-3（审计实测的 Q1 失败根因）。
"""
import pytest

from utils import rerank


def test_sigmoid_bounds():
    assert rerank.sigmoid(0) == pytest.approx(0.5)
    assert rerank.sigmoid(50) > 0.999
    assert rerank.sigmoid(-50) < 0.001


def test_sigmoid_handles_overflow_without_raising():
    """math.exp 在大输入上会 OverflowError，不能让它冒到调用方。"""
    assert rerank.sigmoid(1e5) == 1.0
    assert rerank.sigmoid(-1e5) == 0.0


def test_spread_of_uses_max_minus_min():
    assert rerank.spread_of([3.0, 1.0, 2.0]) == pytest.approx(2.0)
    assert rerank.spread_of([]) == 0.0


def test_saturated_scores_are_not_informative():
    """真实失败样本：英文 MiniLM 给中文 pair 打分，sigmoid 后几乎一样。"""
    raw = [8.5, 8.4, 8.3]
    trustworthy, spread = rerank.is_informative(raw)
    assert trustworthy is False
    assert spread < rerank.RERANK_MIN_SPREAD


def test_discriminative_scores_are_informative():
    """正常可用的精排：正确文档明显高于干扰项。"""
    raw = [8.0, 0.0, -4.0]
    trustworthy, spread = rerank.is_informative(raw)
    assert trustworthy is True
    assert spread > rerank.RERANK_MIN_SPREAD


def test_empty_and_single_candidate_are_trusted():
    """没有候选、或只有一个候选时不存在"乱排"风险，交给调用方按原顺序处理。"""
    assert rerank.is_informative([]) == (True, 0.0)
    assert rerank.is_informative([7.7]) == (True, 0.0)


def test_min_spread_is_configurable():
    raw = [2.0, 4.0]  # sigmoid 后极差约 0.12
    assert rerank.is_informative(raw, min_spread=0.01)[0] is True
    assert rerank.is_informative(raw, min_spread=0.5)[0] is False


def test_normalize_scores_keeps_order_and_squashes_to_unit_interval():
    normalized = rerank.normalize_scores([10.0, 0.0, -10.0])
    assert len(normalized) == 3
    assert all(0.0 <= v <= 1.0 for v in normalized)
    assert normalized == sorted(normalized, reverse=True)


def test_window_ignores_low_scoring_tail():
    """实测场景：top-10 整体极差很大（尾巴里有明显无关的低分文档），
    但**前 3 名之间**分不开——这正是第一版修复漏判的地方。

    只看全局极差会误判成"精排有效"，于是随机抽签般选出的 3 条覆盖了正确的 RRF 顺序；
    加上 window 后才判得出来。
    """
    raw = [8.50, 8.45, 8.40, 1.00, 0.00, -1.00, -2.00, -3.00, -4.00, -5.00]
    assert rerank.is_informative(raw)[0] is True  # 全局极差：看着"有区分度"
    trustworthy, spread = rerank.is_informative(raw, window=3)
    assert trustworthy is False  # 前 3 名饱和 -> 不可信
    assert spread < rerank.RERANK_MIN_SPREAD


def test_window_with_discriminative_top_scores_is_informative():
    raw = [8.0, 2.0, 0.0, -2.0, -4.0]
    assert rerank.is_informative(raw, window=3)[0] is True


def test_window_larger_than_candidates_is_harmless():
    assert rerank.is_informative([8.0, 2.0], window=99)[0] is True


def test_window_of_one_candidate_is_trusted():
    """窗口里只剩 1 条时无从比较，不构成"乱排"风险。"""
    assert rerank.is_informative([8.0, 7.9, 7.8], window=1) == (True, 0.0)


# ---------------------------------------------------------------------------
# 归一化：按模型族选映射（2026-10-13 实测）
#
# 这里钉死的是**实测出来的原始分数**（脚本 measure_rerank_raw.py 直接调
# HuggingFaceCrossEncoder.score()，两个模型都在本机 HF 缓存里，不需要联网）：
#   * MiniLM（英文 MS MARCO）→ 原始 logits：2.7399 / 5.9394 / -1.9723 / 1.9733 / 7.1776
#   * BGE-reranker-base（中文）→ **已经是概率**：0.9993 / 0.000037 / 0.0124 / 0.000037 / 0.7875
# ---------------------------------------------------------------------------

MINILM = "cross-encoder/ms-marco-MiniLM-L-6-v2"
BGE = "BAAI/bge-reranker-base"

#: MiniLM 的 5 个中文样本：依次是"相关/不相关/相关/不相关/相关"
MINILM_RAW = [2.739890, 5.939425, -1.972280, 1.973312, 7.177572]
#: BGE 的同一批样本（第一个是联系方式块 = 相关，值 0.9993）
BGE_RAW = [0.999303, 0.000037, 0.012433, 0.000037, 0.787463]

#: ``api/routers/chat.py`` 的拒答阈值（这里写字面量：单测刻意不 import 那个模块，
#: 它会连带 import config / services）。归一化必须保住"0.5 ⟺ raw=0"这条语义。
CONFIDENCE_THRESHOLD = 0.5


class TestUserReportedRegression:
    """用户实测报的现象：MiniLM 的 rerank_score "全都是 1.00"，3 分和 8 分分不出来。"""

    def test_raw_3_and_8_are_clearly_distinguishable(self):
        low, high = rerank.normalize_scores([3.0, 8.0], model_name=MINILM)
        assert low == pytest.approx(0.6457, abs=0.005)
        assert high == pytest.approx(0.8320, abs=0.005)
        assert high - low > 0.1, "3 分与 8 分必须明显区分"
        assert high < 0.9, "8 分不能被压成 1.00（这正是用户看到的 bug）"

    def test_the_old_mapping_is_what_saturated(self):
        """对照数据：旧映射（T=1）下 3 分 = 0.9526、8 分 = 0.9997，肉眼分不出。"""
        old_low, old_high = (rerank.sigmoid(v) for v in (3.0, 8.0))
        assert old_low > 0.95 and old_high > 0.999
        assert old_high - old_low < 0.05

    def test_measured_minilm_scores_are_no_longer_all_near_one(self):
        """实测 5 个中文样本：旧映射是 0.12~0.9992（两块贴着 1.00），新映射落回中间带。"""
        new = rerank.normalize_scores(MINILM_RAW, model_name=MINILM)
        old = [rerank.sigmoid(v) for v in MINILM_RAW]

        assert max(new) < 0.9                      # 不再"全是 1.00"
        assert all(0.0 < v < 1.0 for v in new)     # 也没有被压到端点
        assert rerank.spread_of(new) > 0.3         # 区分度可读
        assert sum(1 for v in old if v > 0.99) == 2  # 旧映射下有两块 ≥0.99

    #: 真实那次召回（before 取证日志的 debug 分数 0.9929/0.9991/0.9995/0.9998/0.9987，
    #: 用 ln(s/(1-s)) 还原成 raw logits）——它们全挤在 7~8.5
    MINILM_RECALL_RAW = [8.517, 7.600, 7.012, 4.940, 6.644]

    def test_minilm_chinese_ordering_is_still_noise_so_gate_must_fire(self):
        """换映射**不会**让 MiniLM 变准：不相关块(5.94) 依然高于相关块(2.74)。

        这是**有意的**——修复只负责"分数可读且不饱和"，"排序可信不可信"由闸门
        （``is_informative``）负责；中文语料的正解是换成默认的 BGE。
        """
        normalized = rerank.normalize_scores(MINILM_RAW[:2], model_name=MINILM)
        assert normalized[1] > normalized[0]

    def test_real_recall_batch_is_flagged_saturated(self):
        """真实召回那批（raw 全在 7~8.5）：T=1 极差约 0.0007 → 判饱和、保留 RRF 顺序。

        与 before 取证日志里的 ``rerank_no_discrimination spread=0.00078081`` 是同一批。
        注意闸门口径**没有变**（仍 T=1）：修的是展示分数，不是"要不要信这次精排"。
        """
        trustworthy, spread = rerank.is_informative(
            self.MINILM_RECALL_RAW, window=3, model_name=MINILM
        )
        assert trustworthy is False
        assert 0.0 < spread < rerank.RERANK_MIN_SPREAD

    def test_handcrafted_samples_are_genuinely_discriminative(self):
        """对照：手工那 5 个样本 raw 从 -1.97 到 +7.18，闸门判"有区分度"——它没坏。

        MiniLM 在中文上"有时看着能分、有时完全分不清"这种**不稳定**才是要换默认模型的
        原因；闸门只负责"分不清时别乱排"。
        """
        assert rerank.is_informative(MINILM_RAW, window=3, model_name=MINILM)[0] is True


class TestBgeProbabilitiesAreNotSigmoidedTwice:
    """BGE 实测直接返回概率：再套一层 sigmoid 会把 [0,1] 二次压进 [0.5, 0.73]。"""

    def test_probabilities_are_not_rescaled_but_never_reach_one(self):
        """实测那批 BGE 分数：只有"贴着 1.00"的那条被封顶（0.9993 → 0.999），其余原样通过。

        为什么要封顶（2026-10-13 用户复验）：BGE 的极端样本会刚好给出 1.000000，
        界面于是又显示"最高置信度 1.00"；而 1.00 在本系统里是**哨兵值**——
        "这次没做分数门控"（关闭精排时检索层写的就是它）。两者撞脸后，用户再也分不清
        "完美匹配"和"根本没打分"。
        """
        normalized = rerank.normalize_scores(BGE_RAW, model_name=BGE)

        assert normalized[0] == pytest.approx(rerank.MAX_RERANK_SCORE)
        assert normalized[1] == pytest.approx(0.000037)
        assert normalized[2] == pytest.approx(0.012433)
        assert normalized[4] == pytest.approx(0.787463)

    def test_only_the_saturated_end_is_capped(self):
        """封顶只动"贴到 1.00"那一端：0.999 以下一律原样，顺序也不变。"""
        normalized = rerank.normalize_scores([0.4, 0.9993, 0.02], model_name=BGE)

        assert normalized == [pytest.approx(0.4), pytest.approx(0.999), pytest.approx(0.02)]
        assert max(normalized) <= rerank.MAX_RERANK_SCORE

    @pytest.mark.parametrize("raw", [1.0, 0.9999, 1.5, 3.0])
    def test_raw_one_never_comes_back_as_one(self, raw):
        """用户报的那条：raw_score = 1.0（以及任何 ≥0.999 的极端值）→ 输出必须 ≤ 0.999。"""
        out = rerank.normalize_scores([raw], model_name=BGE)[0]

        assert out <= rerank.MAX_RERANK_SCORE, f"raw={raw} 又被贴到了 1.00"
        assert out < 1.0

    def test_gate_still_sees_bge_probabilities(self):
        """封顶不能把闸门弄瞎：那条 0.9993 被削成 0.999 后，极差仍远大于 min_spread。"""
        trustworthy, spread = rerank.is_informative(BGE_RAW, window=3, model_name=BGE)

        assert trustworthy is True
        assert spread > 0.9

    def test_logits_branch_is_not_capped(self):
        """反向锁定：封顶**只**作用于概率分支，logits 的 sigmoid 语义不许被顺手改掉。

        给 sigmoid 结果也封顶会让"极端 logits 饱和"探测失灵（11/10/9 全变 0.999）。
        """
        assert rerank.normalize_scores([40.0], model_name=MINILM)[0] > 0.999

    def test_old_mapping_would_fool_the_confidence_threshold(self):
        """旧行为：无关块 0.000037 --sigmoid--> 0.500009 ≥ 0.5 → 拒答闸门被绕过。"""
        fooled = rerank.sigmoid(BGE_RAW[1])
        assert fooled == pytest.approx(0.500009, abs=1e-5)
        assert fooled >= CONFIDENCE_THRESHOLD, "旧映射下无关块恰好卡在阈值之上"

        fixed = rerank.normalize_scores(BGE_RAW, model_name=BGE)[1]
        assert fixed < CONFIDENCE_THRESHOLD

    def test_dispatch_is_by_model_family(self):
        assert rerank.is_probability_model(BGE) is True
        assert rerank.is_probability_model("BAAI/bge-reranker-large") is True
        assert rerank.is_probability_model(MINILM) is False
        assert rerank.is_probability_model(None) is False
        assert rerank.is_probability_model("") is False

    def test_unknown_model_falls_back_to_logits(self):
        """未知模型按 logits 处理（历史行为）：宁可要求新增模型时显式登记，也不猜。"""
        assert rerank.normalize_scores(
            [3.0], model_name="someone/unknown-reranker"
        ) == pytest.approx([0.6457], abs=0.005)

    def test_clip_unit_interval_handles_out_of_range(self):
        assert rerank.clip_unit_interval(-0.2) == 0.0
        assert rerank.clip_unit_interval(1.7) == 1.0
        assert rerank.clip_unit_interval(0.42) == pytest.approx(0.42)


class TestModelAwareNormalizationContract:
    def test_monotonic_order_is_preserved(self):
        """换映射只改刻度、不改顺序（排序语义不变，否则等于顺手改了检索行为）。"""
        raw = [7.0, 3.0, 0.0, -4.0]
        for model in (MINILM, BGE):
            normalized = rerank.normalize_scores(raw, model_name=model)
            assert normalized == sorted(normalized, reverse=True)

    def test_temp_zero_is_rejected(self):
        with pytest.raises(ValueError):
            rerank.normalize_scores([1.0, 2.0], model_name=MINILM, temperature=0)

    def test_empty_input(self):
        assert rerank.normalize_scores([], model_name=MINILM) == []
        assert rerank.normalize_scores([], model_name=BGE) == []

    def test_display_temperature_never_reaches_one(self):
        """强相关（raw=40）也不许贴到 1.00：贴上去之后"谁更相关"就又看不出来了。"""
        assert rerank.normalize_scores([40.0], model_name=MINILM)[0] < 0.9997
        assert rerank.normalize_scores([20.0], model_name=MINILM)[0] == pytest.approx(0.982, abs=0.002)


class TestGateKeepsTheMostSensitiveMapping:
    """闸门固定用 T=1 的快照：T=5 会把"极端 logits 的饱和"漏判成"有区分度"。"""

    EXTREME = [11.0, 10.0, 9.0]

    def test_extreme_logits_are_still_flagged_as_saturated(self):
        trustworthy, spread = rerank.is_informative(self.EXTREME, window=3, model_name=MINILM)
        assert trustworthy is False
        assert spread < rerank.RERANK_MIN_SPREAD

    def test_display_mapping_would_have_judged_them_informative(self):
        """反证：同一批分数若用展示口径（T=5），极差 0.042 > 0.02 → 会被误判为可信。"""
        display = rerank.normalize_scores(self.EXTREME, model_name=MINILM)
        assert rerank.spread_of(display) > rerank.RERANK_MIN_SPREAD

    def test_probability_model_gate_uses_probabilities(self):
        """BGE 走裁剪分支：0.9993 vs 0.000037 天然可分，不会被 sigmoid 压到 0.50 附近。"""
        trustworthy, spread = rerank.is_informative(BGE_RAW, window=3, model_name=BGE)
        assert trustworthy is True
        assert spread > 0.9


class TestDefaultModelIsAligned:
    """默认值三方同源：utils.rerank ↔ api.schemas ↔ 前端 client.ts（前端由 E2E 钉住）。"""

    def test_default_is_bge(self):
        assert rerank.DEFAULT_RERANK_MODEL == BGE
        assert rerank.is_probability_model(rerank.DEFAULT_RERANK_MODEL) is True

    def test_api_schema_default_matches(self):
        from api.schemas import ChatIn, RERANK_MODEL_CHOICES

        assert ChatIn.model_fields["rerank_model"].default == rerank.DEFAULT_RERANK_MODEL
        assert RERANK_MODEL_CHOICES[0] == rerank.DEFAULT_RERANK_MODEL
        # MiniLM 仍在白名单里（备选），只是不再是默认值
        assert MINILM in RERANK_MODEL_CHOICES
