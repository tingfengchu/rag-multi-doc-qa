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
