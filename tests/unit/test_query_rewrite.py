"""模块 1 的回归测试：**无历史时绝不触发 Query Rewrite**（纯离线，不调用真实 LLM）。

背景（真实踩过的坑）：对话历史按 ``(user_uuid, scope)`` 存在 Redis（TTL 1 小时、
最多 10 轮）。页面刷新 / 切库只清空了前端，服务端那份还在，于是下一次追问会被
"用户看不见的上下文"改写，界面上冒出"已重写为：xxx"。

锁定的契约：

1. ``history`` 为空 / None / 只有空白内容 / 只有一条孤立 user 消息 →
   **原样返回**，且一次改写链都不许构造（用间谍 Prompt 断言，比"结果相等"更严格：
   结果相等有可能是 LLM 恰好也返回了原问题）；
2. 真有问答对时才调用改写链，并去掉模型爱加的引号；
3. 改写链抛异常 → 降级返回原问题（异常绝不能漏进 SSE 生成器，否则前端只看到"连接断开"）。

运行：``.venv\\Scripts\\python.exe -m pytest tests/unit/test_query_rewrite.py -v``
"""
from types import SimpleNamespace

import pytest

from services import query_rewrite_service as qrs


class _SpyChain:
    """假改写链：记录调用载荷，并返回一个"改写过的"问题（带引号，用于验证清洗）。"""

    def __init__(self, calls):
        self._calls = calls

    def invoke(self, payload):
        self._calls.append(payload)
        return SimpleNamespace(content='  "张嘉亮负责哪个项目？"  ')


class _SpyPrompt:
    """间谍 Prompt：``|`` 一被使用就说明改写链真的被构造了。"""

    def __init__(self, calls):
        self._calls = calls

    def __or__(self, other):
        return _SpyChain(self._calls)


@pytest.fixture
def spy(monkeypatch):
    """把改写链整体替换成间谍（不碰 rewrite_llm，避免任何真实模型/网络依赖）。"""
    calls = []
    monkeypatch.setattr(qrs, "REWRITE_PROMPT", _SpyPrompt(calls))
    return calls


#: 一个典型的"追问"（含代词，最容易被错误改写的那种）
QUESTION = "他负责哪个项目？"

#: 一条正常的问答历史
PAIR = [
    {"role": "user", "content": "张嘉亮是谁？"},
    {"role": "assistant", "content": "张嘉亮是算法工程师。"},
]


class TestNoUsableHistoryNeverRewrites:
    def test_empty_history(self, spy):
        assert qrs.rewrite_query(QUESTION, []) == QUESTION
        assert spy == [], "无历史却构造了改写链：追问会被看不见的上下文改写"

    def test_none_history(self, spy):
        assert qrs.rewrite_query(QUESTION, None) == QUESTION
        assert spy == []

    def test_blank_entries_are_not_history(self, spy):
        history = [
            {"role": "user", "content": "   "},
            {"role": "assistant", "content": ""},
        ]
        assert qrs.rewrite_query(QUESTION, history) == QUESTION
        assert spy == []

    def test_dangling_user_turn_is_not_history(self, spy):
        """只有一条 user 消息（上一轮中途失败的残骸）→ 不足以做指代消解。"""
        assert qrs.rewrite_query(QUESTION, [{"role": "user", "content": "张嘉亮是谁？"}]) == QUESTION
        assert spy == []


class TestHasUsableHistory:
    def test_matrix(self):
        assert qrs.has_usable_history(None) is False
        assert qrs.has_usable_history([]) is False
        assert qrs.has_usable_history([{"role": "user", "content": "问题"}]) is False
        assert qrs.has_usable_history([{"role": "assistant", "content": "回答"}]) is False
        assert qrs.has_usable_history(PAIR) is True

    def test_tolerates_malformed_entries(self):
        """历史是 Redis 里的 JSON，格式漂了也不能 500 —— 一律当"没有历史"。"""
        assert qrs.has_usable_history([None, "不是字典", {"role": "user"}]) is False


class TestWithUsableHistory:
    def test_rewrites_and_strips_quotes(self, spy):
        assert qrs.rewrite_query(QUESTION, PAIR) == "张嘉亮负责哪个项目？"
        assert len(spy) == 1, "有历史时应恰好调用一次改写链"
        assert "张嘉亮是谁？" in spy[0]["history"]
        assert spy[0]["question"] == QUESTION

    def test_only_recent_turns_are_sent(self, spy):
        long_history = PAIR * (qrs.MAX_HISTORY_TURNS + 4)
        qrs.rewrite_query(QUESTION, long_history)
        assert len(spy[0]["history"].splitlines()) == qrs.MAX_HISTORY_TURNS

    def test_llm_failure_degrades_to_original_question(self, monkeypatch):
        class FailingPrompt:
            def __or__(self, other):
                raise RuntimeError("api down")

        monkeypatch.setattr(qrs, "REWRITE_PROMPT", FailingPrompt())
        assert qrs.rewrite_query(QUESTION, PAIR) == QUESTION

    def test_empty_model_output_degrades_to_original_question(self, monkeypatch):
        """模型返回空串时不能把空问题交给检索（否则必然"未找到相关信息"）。"""

        class EmptyChain:
            def invoke(self, payload):
                return SimpleNamespace(content="   ")

        class EmptyPrompt:
            def __or__(self, other):
                return EmptyChain()

        monkeypatch.setattr(qrs, "REWRITE_PROMPT", EmptyPrompt())
        assert qrs.rewrite_query(QUESTION, PAIR) == QUESTION


class TestOverRewriteGuard:
    """实测的过度改写：『张嘉亮主修课程』→『张嘉亮主修了哪些课程』（问题本身已完整）。

    为什么要"规则先挡一道"而不是"全交给 LLM 判断"：LLM 对"要不要顺手润色一下"没有
    稳定边界，而词面一动，BM25 那条臂的关键词命中就跟着变——这是检索质量问题，
    不能交给一个每天可能给出不同答案的判据。
    """

    #: 用户报的那句：既没有代词，也不是省略句（长度 7 字）
    COMPLETE = "张嘉亮主修课程"

    def test_reported_case_never_reaches_the_llm(self, spy):
        assert qrs.rewrite_query(self.COMPLETE, PAIR) == self.COMPLETE
        assert spy == [], "自洽问题被送去改写了：模型会顺手补全，检索词面跟着偏"

    @pytest.mark.parametrize(
        "question",
        [
            "张嘉亮主修课程",
            "张嘉亮的手机号和邮箱分别是多少？",
            "BM25 与向量检索的结果怎么融合",
        ],
    )
    def test_other_complete_questions_also_skip(self, spy, question):
        assert qrs.rewrite_query(question, PAIR) == question
        assert spy == []

    @pytest.mark.parametrize(
        "question",
        [
            "他负责哪个项目？",   # 代词
            "她的邮箱呢？",       # 代词 + 省略助词
            "那这个项目呢？",     # 指示代词
            "再说说",             # 太短，不敢在这里判
        ],
    )
    def test_real_follow_ups_still_go_to_the_llm(self, spy, question):
        """指代追问与省略句必须仍然交给 LLM——否则"指代消解"这个功能就废了。"""
        qrs.rewrite_query(question, PAIR)
        assert len(spy) == 1

    def test_skipped_but_reported_as_self_contained(self, monkeypatch):
        """"跳过"与"跳过是因为问题已完整"要能区分（meta.rewrite_skipped 靠它排障）。"""
        skipped = []
        monkeypatch.setattr(
            qrs.logger, "info",
            lambda event, extra=None: skipped.append((event, (extra or {}).get("reason"))),
        )
        qrs.rewrite_query(self.COMPLETE, PAIR)
        assert skipped == [("rewrite_skipped", "self_contained")]


class TestIsSelfContained:
    """规则本身的矩阵（纯函数，将来改规则时先看这里）。"""

    def test_complete_questions(self):
        assert qrs.is_self_contained("张嘉亮主修课程") is True
        assert qrs.is_self_contained("张嘉亮的手机号和邮箱分别是多少？") is True
        assert qrs.is_self_contained("  张嘉亮主修课程  ") is True  # 首尾空白不是信息

    @pytest.mark.parametrize("question", ["他是谁", "她负责什么", "它怎么用", "这个项目呢", "那门课讲什么"])
    def test_pronouns_and_demonstratives_are_not_self_contained(self, question):
        assert qrs.is_self_contained(question) is False

    @pytest.mark.parametrize("question", ["张嘉亮的手机号呢", "张嘉亮主修课程吗", "还有别的吧"])
    def test_ellipsis_tails_are_not_self_contained(self, question):
        assert qrs.is_self_contained(question) is False

    @pytest.mark.parametrize("question", ["再说说", "", "   ", "？"])
    def test_too_short_or_empty_is_not_judged_self_contained(self, question):
        assert qrs.is_self_contained(question) is False


class TestSkipReason:
    def test_reports_which_gate_fired(self):
        assert qrs.skip_reason("他负责哪个项目？", PAIR) is None        # 真的会去改写
        assert qrs.skip_reason("张嘉亮主修课程", PAIR) == "self_contained"
        assert qrs.skip_reason("张嘉亮主修课程", []) == "no_history"    # 老闸门优先


class TestPromptWasTightened:
    def test_prompt_allows_only_two_kinds_of_rewrite(self):
        """prompt 是"加严"的载体：把它改回"顺手补全"必须让这条测试红。"""
        text = "\n".join(
            m.content
            for m in qrs.REWRITE_PROMPT.format_messages(
                history="u: q\na: a", question="q", entities="张嘉亮"
            )
        )
        assert "原样输出" in text
        assert "代词" in text and "省略句" in text
        assert "张嘉亮主修课程" in text, "实测反例要写进 prompt，模型才不会顺手补全"

    def test_prompt_forbids_leaving_a_pronoun_behind(self):
        """2026-10-13 实测："只把'呢？'抹掉、代词留着"也算没改写完 → 必须写进 prompt。"""
        text = "\n".join(
            m.content
            for m in qrs.REWRITE_PROMPT.format_messages(
                history="u: q\na: a", question="q", entities="张嘉亮"
            )
        )
        assert "不许再留下任何代词" in text
        assert "他的实习经历呢？" in text and "张嘉亮的实习经历" in text

    def test_prompt_receives_the_entity_candidates(self):
        """候选实体要喂给模型：它得知道"他"可以换成谁，而不是自己编一个人名。"""
        messages = qrs.REWRITE_PROMPT.format_messages(
            history="u: q\na: a", question="q", entities="张嘉亮、RAG"
        )
        system_text = messages[0].content

        assert "张嘉亮、RAG" in system_text
        assert "{entities}" not in system_text, "占位符没被填上（模型只会看到一个花括号）"

def _stub_llm(monkeypatch, content: str):
    """把改写链换成"固定输出"的替身。

    真实 LLM 的**语义质量**归 E2E 管（``e2e/rewrite-followup.spec.ts``）；
    单测这里锁的是它周边那套东西：闸门有没有放行、历史/候选实体有没有喂进去、
    输出有没有被校验与兜底。
    """

    class FixedChain:
        def invoke(self, payload):
            return SimpleNamespace(content=content)

    class FixedPrompt:
        def __or__(self, other):
            return FixedChain()

    monkeypatch.setattr(qrs, "REWRITE_PROMPT", FixedPrompt())


class TestFollowUpPronounMustBeResolved:
    """用户报的 P0（2026-10-13）：追问「他的实习经历呢？」改写完"他"还在。

    现象链：规则闸门放行（含代词）→ LLM 只把"呢？"抹掉 → 检索去查"他"
    → 置信度 0.12 → 拒答。所以这里锁两件事：

    1. 这条追问**必须**过闸门（不许被 ``is_self_contained`` 误判为自包含）；
    2. LLM 输出里还有代词时，必须用历史实体兜底替换（不能只靠"希望模型配合"）。
    """

    #: 用户报的历史（真实形状：Redis 里存的就是 ``{"role", "content"}``）
    HISTORY = [
        {"role": "user", "content": "张嘉亮是谁？"},
        {"role": "assistant", "content": "张嘉亮是算法工程师，负责 RAG 项目。"},
    ]
    FOLLOW_UP = "他的实习经历呢？"

    def test_follow_up_is_not_mistaken_for_self_contained(self):
        assert qrs.is_self_contained(self.FOLLOW_UP) is False
        assert qrs.skip_reason(self.FOLLOW_UP, self.HISTORY) is None, "这条追问被规则闸门拦下了"

    def test_entity_extraction_finds_the_person(self):
        assert qrs.extract_entities(self.HISTORY)[0] == "张嘉亮"

    def test_unresolved_pronoun_is_detected(self):
        assert qrs.unresolved_pronouns("他的实习经历") == ["他"]
        assert qrs.unresolved_pronouns("张嘉亮的实习经历") == []

    def test_other_is_not_treated_as_an_anaphor(self):
        """"其他方案"含"他"字但不是指代：按字面判会把它改成"张嘉亮方案"。"""
        assert qrs.unresolved_pronouns("其他方案是什么") == []

    def test_llm_no_op_is_repaired_by_the_fallback(self, monkeypatch):
        """**用户报的那条**：LLM 输出「他的实习经历」→ 必须变成「张嘉亮的实习经历」。"""
        _stub_llm(monkeypatch, "他的实习经历")

        rewritten = qrs.rewrite_query(self.FOLLOW_UP, self.HISTORY)

        assert rewritten == "张嘉亮的实习经历"
        assert qrs.unresolved_pronouns(rewritten) == []

    def test_compliant_llm_output_is_used_as_is(self, monkeypatch):
        """模型做对了就原样采信——不许再叠一层替换，把"张嘉亮的实习经历"改坏。"""
        _stub_llm(monkeypatch, "张嘉亮的实习经历是什么？")

        assert qrs.rewrite_query(self.FOLLOW_UP, self.HISTORY) == "张嘉亮的实习经历是什么？"

    def test_history_without_entities_does_not_invent_one(self, monkeypatch):
        """历史里抽不到对象时**不编造**：原样保留 + 留一条 warning（可观测，不假装成功）。"""
        warnings = []
        monkeypatch.setattr(
            qrs.logger, "warning", lambda event, extra=None: warnings.append(event)
        )
        _stub_llm(monkeypatch, "他的实习经历")
        history = [
            {"role": "user", "content": "他是谁？"},
            {"role": "assistant", "content": "他是工程师。"},
        ]

        assert qrs.rewrite_query(self.FOLLOW_UP, history) == "他的实习经历"
        assert "rewrite_pronoun_unresolved" in warnings

    def test_fallback_is_logged_with_the_entity_it_used(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            qrs.logger, "info", lambda event, extra=None: events.append((event, extra or {}))
        )
        _stub_llm(monkeypatch, "他的实习经历")

        qrs.rewrite_query(self.FOLLOW_UP, self.HISTORY)

        fallback = [extra for event, extra in events if event == "rewrite_pronoun_fallback"]
        assert fallback, "兜底替换发生了却没留痕：线上就查不出这次是谁救的场"
        assert fallback[0]["entity"] == "张嘉亮"
        assert fallback[0]["fixed"] == "张嘉亮的实习经历"

    def test_plural_pronoun_is_replaced_before_the_singular_one(self):
        """"他们"必须先于"他"匹配，否则会得到"张嘉亮们"。"""
        assert qrs._resolve_pronoun("他们的实习经历", "张嘉亮") == "张嘉亮的实习经历"


class TestEntityCandidatesFeedThePrompt:
    def test_candidates_are_passed_to_the_llm(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            qrs, "REWRITE_PROMPT", _RecordingPrompt(calls, "张嘉亮的实习经历")
        )

        qrs.rewrite_query("他的实习经历呢？", TestFollowUpPronounMustBeResolved.HISTORY)

        assert calls[0]["entities"].startswith("张嘉亮"), "候选实体没喂给模型"
        assert calls[0]["question"] == "他的实习经历呢？"
        assert "张嘉亮是谁？" in calls[0]["history"]


class _RecordingPrompt:
    """记录 invoke 载荷的替身 prompt。"""

    def __init__(self, calls, content):
        self._calls = calls
        self._content = content

    def __or__(self, other):
        return _RecordingChain(self._calls, self._content)


class _RecordingChain:
    def __init__(self, calls, content):
        self._calls = calls
        self._content = content

    def invoke(self, payload):
        self._calls.append(payload)
        return SimpleNamespace(content=self._content)

