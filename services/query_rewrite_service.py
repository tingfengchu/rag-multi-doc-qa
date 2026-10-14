"""Query Rewriting：多轮对话的指代消解（把"他 / 这个 / 它"补成完整问题）。

⚠️ 铁律：**拿不到可用历史就绝不改写**——直接原样返回，连 LLM 都不构造。
为什么这条闸门要写死在服务层：历史是按 ``(user_uuid, scope)`` 存在 Redis 里的
（``chat_history:*``，TTL 1 小时、最多 10 轮）。页面刷新只清空了前端，
服务端那份还在——此时若放行改写，用户会看到凭空冒出来的"已重写为：xxx"
（独立问题被模型顺手改写/总结，甚至改错）。

两道防线配合使用：
* 前端：刷新 / 切库 / 切文件过滤时同步 ``DELETE /api/history``（ChatPage.resetConversation）；
* 后端：本函数只认"真正含问答对的历史"，其余一律原样返回（本文件即最后一道闸）。

---------------- 2026-10-13 追加：过度改写（问题本身已完整）----------------

实测样本：问「**张嘉亮主修课程**」被改写成「张嘉亮主修了哪些课程」——
既没有代词、也不是省略句，却被模型"顺手补全"。这种改写对检索是**有害**的：
BM25 命中的正是"主修课程"这几个字，词面被动过，关键词臂就跟着偏；界面上还会
冒出"已重写为：…"，让用户以为系统听错了问题。

于是闸门升级为**两段**（顺序不能反）：

1. ``is_self_contained()`` 规则判定（纯函数、可离线单测）：不含代词/指代标记、
   句尾不是"呢/吗"这类省略助词、长度足够 → **直接跳过改写**，一次 LLM 都不调；
2. 剩下的（含代词 / 省略句）才交给 LLM 做指代消解，且 prompt 已被加严：
   只有"代词替换"和"省略补全"两种改写被允许，其余必须原样输出。

为什么规则而不是"全交给 LLM 判断"：LLM 对"要不要顺手润色一下"没有稳定的边界，
同一个问题可能今天改写、明天不改——这类不确定性会直接体现在检索结果上；
而"有没有代词/是不是省略句"是可以写死并测死的。

---------------- 2026-10-13 追加：LLM 说改了、其实没消解（最严重的一类）----------------

实测样本：历史是「张嘉亮是谁？」，追问「**他的实习经历呢？**」，
LLM 输出「**他的实习经历**」——只把"呢？"抹掉，"他"原封不动。

这种"假改写"比"不改写"更坏：

* 界面上照样显示「已重写为：他的实习经历」，用户以为系统听懂了；
* 检索却真的去查"他"，什么都命中不了（实测置信度 0.12 → 拒答）；
* 而"改写前后是不是同一个字符串"这种判据**抓不到它**——字符串确实变了。

所以 LLM 的输出必须**按语义校验**，且校验失败要有**确定性兜底**：

1. ``unresolved_pronouns()`` 用词性标注判断"还有没有没消解的指代"。
   不能用 ``"他" in text``：'其他' 含"他"字但不是指代，拿它去替换会把
   "其他方案"改成"张嘉亮方案"——比不改还坏；
2. 还有指代 → ``extract_entities()`` 从历史里抽具体对象（jieba 词性：人名 nr /
   专名 nz / 英文词 eng），再用**规则**替换（``_resolve_pronoun``）。

为什么不"再问一次 LLM"：验收要的是"指代一定被消解"，不是"再赌一次模型的运气"。
LLM 负责理解（它能补省略、能处理没有现成词表的情况），规则负责兜底（它不随机）；
两条路的去向都写进 logger（``rewrite_pronoun_fallback`` / ``rewrite_pronoun_unresolved``），
所以"模型到底行不行"依然可观测，而不是被兜底掩盖成"看起来都正常"。
"""
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from config import settings
from utils.logger import logger

#: 送给改写模型的最近条数：历史本身最多 10 轮（20 条），这里再砍一刀——越近越相关
MAX_HISTORY_TURNS = 8

#: 指代/省略标记词：**只有出现它们**才可能真的需要结合历史改写。
#: 故意取最小集（宁可多问一次 LLM，也不能漏掉真正指代）：漏掉指代追问的代价是
#: "答非所问"（检索去查"他"），多问一次的代价只是一次 LLM 调用——prompt 里已要求
#: "完整就原样输出"，所以多问一次不会带来错误改写。
PRONOUN_MARKERS = (
    "他", "她", "它", "他/她", "他们", "她们", "它们",
    "这", "那", "该", "其", "此", "上述", "刚才", "前面",
)

#: 省略句的句尾助词（"张嘉亮的手机号呢？" / "那他的邮箱吗"）。
ELLIPSIS_TAILS = ("呢", "吗", "吧", "么")

#: 需要**消解**的指代代词。与 ``PRONOUN_MARKERS`` 的分工：
#: 后者是"要不要叫 LLM"的粗筛（宁可多问一次），这里是"有没有真的消解掉"的判据
#: ——判据必须准，判错会直接改写用户的问题。
#: 长词放前面：替换时先试"他们"再试"他"，否则"他们"会被替换成"张嘉亮们"。
#: 刻意**不含** 谁/哪/什么 这类疑问代词：它们是问题本身的焦点，替换掉就把问题改坏了。
ANAPHORIC_PRONOUNS = ("他们", "她们", "它们", "他", "她", "它")

#: 从历史里抽"可被指代的对象"时认的词性（jieba posseg 实测）：
#: nr=人名（"张嘉亮"→nr）、nrt=音译人名、nz=其他专名、eng=英文词（"SAGE AI"这类）。
ENTITY_FLAGS = ("nr", "nrt", "nz", "eng")

#: 候选实体长度上限（超过就别要了：多半是模型把一整句切成了一个"专名"）。
MAX_ENTITY_LENGTH = 8

#: 候选实体个数上限（prompt 里列太多反而让模型分心）。
MAX_ENTITY_CANDIDATES = 5

#: 自洽问句的最小长度（按去掉标点后的字符数）：更短的（"再说说"、"详细点"）
#: 大概率是省略句，交给 LLM 判断，不在这里武断跳过。
MIN_SELF_CONTAINED_LENGTH = 6

rewrite_llm = ChatOpenAI(
    model="glm-4-flash",
    temperature=0,
    api_key=settings.zhipu_api_key,
    base_url=settings.openai_api_base,
)

REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "你是一个查询重写助手。你到底只有两种合法改写，其余一律原样输出。\n"
     "⚠️ 绝对红线（极其重要）：\n"
     "1. 只要用户的提问本身已经完整、独立（如'张嘉亮是谁？'、'张嘉亮主修课程'），"
     "必须【原样输出】，**连语序、用词都不许改**。把'张嘉亮主修课程'写成"
     "'张嘉亮主修了哪些课程'这种'顺手补全'是**错误**的：词面一变，关键词检索就跟着偏，"
     "用户还会以为系统听错了他的问题。\n"
     "2. 只有下列两种情况才允许结合历史改写：\n"
     "   (a) 提问含代词（'他/她/它/这/那'等）→ 把代词换成历史里出现过的具体对象；\n"
     "   (b) 提问是省略句（句尾是'呢/吗/吧'，或只有几个字，如'那他的邮箱呢？'、'再说详细点'）"
     "→ 补上被省略的主语/宾语。\n"
     "3. 除 (a)(b) 外，一律原样输出；不要总结、不要添加背景、不要润色。\n"
     "4. 你只负责改写问题，绝对禁止输出任何答案内容！\n"
     "5. 只输出改写后的问题，不要解释，不要加引号。\n"
     "6. ⚠️ 改写完**不许再留下任何代词**：'他的实习经历呢？' 必须写成"
     "'张嘉亮的实习经历'——只把句尾的'呢？'删掉**不算改写完成**。\n"
     "   本次历史里可用的具体对象（只能从中选，不许自己编）：{entities}"),
    ("human", "【历史对话】\n{history}\n\n【追问】\n{question}\n\n【改写后的问题】")
])


def is_self_contained(question: str) -> bool:
    """问题本身是否已经完整独立（→ 不需要、也**不允许**结合历史改写）。

    规则（纯函数，可离线单测；实测的过度改写样本见模块 docstring）：

    * 含代词/指代标记（他/她/它/这/那/该/其/此…）→ 不是自洽的；
    * 句尾是省略助词（呢/吗/吧/么）→ 是追问式省略句；
    * 太短（去掉标点后 < ``MIN_SELF_CONTAINED_LENGTH`` 字）→ 不敢判，交给 LLM；
    * 其余 → 自洽（如"张嘉亮主修课程"、"张嘉亮的手机号和邮箱分别是多少"）。
    """
    raw = (question or "").strip()
    # 先剥掉句尾标点再判"省略助词"：'那他的邮箱呢？' 的句尾标记在标点**之前**
    text = raw.rstrip("？?！!。.、,，；;：: ")
    if not text:
        return False
    if any(marker in text for marker in PRONOUN_MARKERS):
        return False
    if text.endswith(ELLIPSIS_TAILS):
        return False
    return len(text) >= MIN_SELF_CONTAINED_LENGTH


def has_usable_history(history) -> bool:
    """历史里是否存在**可用于指代消解**的问答对。

    只有 user 消息（或内容全是空白）的历史视为不可用：那多半是上一轮请求
    中途失败留下的残骸，拿它去改写只会凭空编造上下文。
    """
    if not history:
        return False

    def _has(role: str) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("role") == role
            and str(item.get("content") or "").strip()
            for item in history
        )

    return _has("user") and _has("assistant")


def skip_reason(question: str, history) -> str | None:
    """改写被跳过的原因；返回 ``None`` = 这次真的会去调 LLM。

    抽成公开函数是为了让 ``api/routers/chat.py`` 的 ``meta.rewrite_skipped``
    如实报告原因（``no_history`` / ``self_contained``），而不是只报"没改写"——
    排查"为什么这次没做指代消解"时，这两者的结论完全相反。
    """
    if not has_usable_history(history):
        return "no_history"
    if is_self_contained(question):
        return "self_contained"
    return None


def _posseg(text: str) -> list:
    """jieba 词性标注（``[(词, 词性), ...]``）；**装不上或出错就返回空**。

    为什么用 ``posseg`` 而不是 ``jieba.cut``：本模块要区分"他"（代词 r）与"其他"
    （会被切成一整个词）、"张嘉亮"（人名 nr）与普通名词——只有词性标注能给这个信息。

    兜底返回空列表：词性标注是**增强**（没有它，LLM 那条主路照样工作），
    绝不允许它把一次问答搞崩。
    """
    try:
        import jieba.posseg as posseg
    except Exception as exc:  # pragma: no cover - 依赖缺失的环境
        logger.warning("posseg_unavailable", extra={"err": str(exc)})
        return []
    try:
        return [(word, flag) for word, flag in posseg.cut(text or "") if word.strip()]
    except Exception as exc:  # pragma: no cover - jieba 内部异常
        logger.warning("posseg_failed", extra={"err": str(exc)})
        return []


def extract_entities(history, limit: int = MAX_ENTITY_CANDIDATES) -> list:
    """从历史问答里抽**可被指代的具体对象**（人名/专名/英文词），最近的排最前。

    顺序即优先级：指代默认指的是"最近提到的那个对象"，所以**倒序扫历史**
    （只记首次出现的顺序）。
    """
    candidates: list = []
    for item in reversed(list(history or [])):
        if not isinstance(item, dict):
            continue
        for word, flag in _posseg(str(item.get("content") or "")):
            if flag not in ENTITY_FLAGS or len(word) > MAX_ENTITY_LENGTH:
                continue
            if word not in candidates:
                candidates.append(word)
    return candidates[:limit]


def unresolved_pronouns(text: str) -> list:
    """改写结果里**还没消解**的指代（按词性判，避免把"其他"里的"他"当指代）。

    返回空列表 = 这次改写可以接受；非空 = LLM 只是把语气词抹掉了、指代还在
    （实测样本见模块 docstring），调用方必须兜底。
    """
    return [
        word for word, flag in _posseg(text)
        if flag == "r" and word in ANAPHORIC_PRONOUNS
    ]


def _resolve_pronoun(question: str, entity: str) -> str:
    """把句中的指代换成具体对象（兜底改写）：'他的实习经历' → '张嘉亮的实习经历'。

    只替换**第一处**：一句话里两次指代且指同一对象时，剩下那处留给下一次校验，
    避免把"他和她"这种并列指代一律换成同一个人。
    """
    for pronoun in ANAPHORIC_PRONOUNS:
        if pronoun in question:
            return question.replace(pronoun, entity, 1)
    return question


def _ensure_pronoun_resolved(question: str, rewritten: str, history) -> str:
    """LLM 没消解掉指代时**必须自救**：用历史里的实体做确定性替换（理由见模块 docstring）。

    校验的是"**还有没有指代**"，而不是"和原文是否不同"——后者会被"只把'呢'抹掉"
    这种假改写骗过去，而那正是用户报的 bug。
    """
    remaining = unresolved_pronouns(rewritten)
    if not remaining:
        return rewritten

    candidates = extract_entities(history)
    if not candidates:
        # 历史里抽不到可用对象（例如全是"他问了他"）：不编造，原样保留并留痕，
        # 让"这次没能消解"可观测，而不是假装成功。
        logger.warning(
            "rewrite_pronoun_unresolved",
            extra={"question": question, "rewritten": rewritten, "pronouns": remaining},
        )
        return rewritten

    fixed = _resolve_pronoun(rewritten, candidates[0])
    logger.info(
        "rewrite_pronoun_fallback",
        extra={
            "question": question,
            "rewritten": rewritten,
            "fixed": fixed,
            "entity": candidates[0],
            "pronouns": remaining,
        },
    )
    return fixed


def rewrite_query(question: str, history: list) -> str:
    """需要指代消解 → 交给 LLM，**并校验它真的消解了**；否则原样返回（零 LLM 调用）。

    跳过路径有两类（``skip_reason``）：没有可用历史（老闸门）、问题本身已完整
    （上一轮的过度改写防护）。两者都不该进这道函数。

    LLM 之后还有一道 ``_ensure_pronoun_resolved``：模型可靠时不做事，
    模型"只抹语气词、留着代词"时用历史实体兜底——**这是"能不能消解"的保证**，
    而不是"希望模型这次配合"。理由见模块 docstring 最后一节。
    """
    reason = skip_reason(question, history)
    if reason:
        logger.info(
            "rewrite_skipped", extra={"reason": reason, "question": question}
        )
        return question

    recent = history[-MAX_HISTORY_TURNS:]
    history_text = "\n".join(
        f"{item.get('role')}: {item.get('content')}" for item in recent
    )
    # 候选实体同时喂给 prompt（让模型第一遍就尽量做对）与兜底替换（模型没做对时）：
    # 两者用**同一份**候选，避免"prompt 里说有 A、兜底却换成 B"这种自相矛盾。
    candidates = extract_entities(history)
    entities_text = "、".join(candidates) if candidates else "（历史里没有可用对象，此时保持原样）"

    try:
        chain = REWRITE_PROMPT | rewrite_llm
        response = chain.invoke(
            {"history": history_text, "question": question, "entities": entities_text}
        )
        rewritten = response.content.strip().strip('"').strip("'")
        rewritten = _ensure_pronoun_resolved(question, rewritten, history)
        # 用 logger 而不是 print：GBK 控制台下 print(emoji) 会抛 UnicodeEncodeError，
        # 在 SSE 生成器里会被当成"对话失败"（历史坑，见 commit b8242cc）。
        logger.info(
            "query_rewritten",
            extra={
                "question": question,
                "rewritten": rewritten,
                "changed": rewritten != question,
                # entities 一起记：没消解成功时，第一件要查的就是"当时候选里到底有什么"
                "entities": candidates,
            },
        )
        return rewritten if rewritten else question
    except Exception as e:
        logger.warning("rewrite_failed", extra={"err": str(e), "question": question})
        return question