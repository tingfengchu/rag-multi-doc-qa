"""语义缓存 ``rerank_score`` 的写入/读取契约（纯离线：内存假 Redis，不连真服务）。

模块 2 修的失真：缓存里没存分数，命中缓存时 ``done`` 载荷只能写死 1.0，
于是同一条答案"首次回答 0.62 / 命中缓存 1.00"——缓存看起来比真实回答更可信。

这里锁定的契约：

1. ``set_cached_answer`` 必须把 ``rerank_score`` 与答案一起落盘，``get_cached_answer``
   原样带回（4 位小数归一，与 ``done`` 载荷同精度）；
2. 字段**一定存在**：本次改造前写入的老缓存 → ``None``（语义"未知"），**不许**伪造 1.0；
3. 脏数据（非法 JSON / 非 dict / 非数字）只当未命中或降级成 ``None``，绝不抛异常——
   缓存是加速手段，不该把一次问答拖垮；
4. ``done`` 载荷的 ``max_score`` 能吃 ``None``（老缓存），真实分数不被抹平；
5. ``api/routers/chat.py`` 不许再出现"缓存命中 → 1.0"的硬编码（源码级回归防线）。

运行：``.venv\\Scripts\\python.exe -m pytest tests/unit -v``
"""
import fnmatch
import json
import pathlib

import pytest

from api.routers import chat
from services import redis_service

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeRedis:
    """只实现缓存路径用到的 ``set(..., ex=)`` / ``get`` / ``scan_iter`` / ``delete``。

    不为一个单测引入 fakeredis 依赖。``scan_iter`` 与批量 ``delete`` 是精确失效
    （删文件 → ``invalidate_cache_by_file_uuid``）必需的：替身缺了它们，那组用例就只剩
    "函数被调用过"这一层断言，而真正会出错的是**匹配逻辑**（删多了 / 删少了）。
    """

    def __init__(self):
        self.strings = {}
        self.ttls = {}
        #: ``delete`` 被调用的次数：没有命中时必须**一次都不发**（真 Redis 上空 DEL 是命令错误）
        self.delete_calls = 0

    # 与 api/sessions.py、services/redis_service.py 保持一致：都用 set(..., ex=)，
    # 不用已弃用的 setex——测试替身若还留着一个"只有 setex"的壳子，
    # 将来谁把生产代码改回 setex 都不会被发现。
    def set(self, key, value, ex=None, **kwargs):
        self.strings[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key):
        return self.strings.get(key)

    def scan_iter(self, match=None):
        """按通配符扫 key（``invalidate_cache_by_file_uuid`` 只扫 ``cache:*``）。"""
        for key in list(self.strings):
            if fnmatch.fnmatch(key, match or "*"):
                yield key

    def delete(self, *keys):
        self.delete_calls += 1
        removed = 0
        for key in keys:
            if self.strings.pop(key, None) is not None:
                self.ttls.pop(key, None)
                removed += 1
        return removed


@pytest.fixture()
def fake(monkeypatch):
    """``redis_service`` 用的是模块级连接 ``r``（不是依赖注入），所以直接换掉它。"""
    client = FakeRedis()
    monkeypatch.setattr(redis_service, "r", client)
    return client


def _seed_raw(fake, question, payload):
    """绕过写入函数，直接塞入"Redis 里已经存在"的内容（模拟老缓存 / 脏数据）。"""
    key = redis_service._build_cache_key("public", question, "all")
    fake.strings[key] = payload
    return key


def _chat_in(question="q"):
    return chat.ChatIn(question=question)


class TestScoreRoundTrip:
    def test_written_score_is_read_back_unchanged(self, fake):
        """需求核心：写入的 rerank_score 与读出来的必须一致。"""
        redis_service.set_cached_answer(
            "public", "张嘉亮是谁", "他是算法工程师", [], [], rerank_score=0.8142
        )

        cached = redis_service.get_cached_answer("public", "张嘉亮是谁")

        assert cached is not None
        assert cached[redis_service.CACHE_SCORE_KEY] == 0.8142
        assert cached["answer"] == "他是算法工程师"

    def test_score_precision_matches_done_payload(self, fake):
        """``done`` 载荷保留 4 位小数，缓存也得同精度，否则"首次 0.8142 / 缓存 0.81423"像是变了。"""
        redis_service.set_cached_answer("public", "q", "a", [], [], rerank_score=0.81423456)

        assert redis_service.get_cached_answer("public", "q")["rerank_score"] == 0.8142

    def test_score_field_always_present_even_when_not_provided(self, fake):
        """字段必须存在（哪怕是 None）：调用方不该靠 ``in`` 判断，漏一次就退回硬编码。"""
        redis_service.set_cached_answer("public", "q", "a", [], [])

        cached = redis_service.get_cached_answer("public", "q")

        assert redis_service.CACHE_SCORE_KEY in cached
        assert cached["rerank_score"] is None

    def test_scores_stay_bound_to_their_own_entry(self, fake):
        """不同问题 / 文件过滤各存各的分数，不能串味（缓存 key 的隔离性）。"""
        redis_service.set_cached_answer("public", "q1", "a1", [], [], rerank_score=0.9)
        redis_service.set_cached_answer("public", "q2", "a2", [], [], rerank_score=0.2)
        redis_service.set_cached_answer(
            "public", "q1", "a3", [], [], filter_key="file-1", rerank_score=0.5
        )

        assert redis_service.get_cached_answer("public", "q1")["rerank_score"] == 0.9
        assert redis_service.get_cached_answer("public", "q2")["rerank_score"] == 0.2
        assert redis_service.get_cached_answer("public", "q1", filter_key="file-1")["rerank_score"] == 0.5

    def test_other_payload_fields_are_untouched(self, fake):
        """回归：别为了加一个字段把引用 / 调试块弄丢（前端要靠它们渲染）。"""
        citations = [{"source": "resume.pdf", "page": 2}]
        debug_info = [{"length": 120, "is_parent": True, "preview": "…", "rerank_score": 0.7}]

        redis_service.set_cached_answer("public", "q", "a", citations, debug_info, rerank_score=0.7)

        cached = redis_service.get_cached_answer("public", "q")
        assert cached["citations"] == citations
        assert cached["debug_info"] == debug_info

    def test_ttl_is_still_applied(self, fake):
        """改造中别把 TTL 弄丢：丢了答案就永不过期，知识库更新后一直误导人。"""
        redis_service.set_cached_answer("public", "q", "a", [], [], rerank_score=0.5)

        key = redis_service._build_cache_key("public", "q", "all")
        assert fake.ttls[key] == 86400


class TestLegacyAndDirtyPayloads:
    def test_legacy_entry_without_score_reads_as_none(self, fake):
        """本次改造前写入的缓存没有这个字段 → None（未知），而不是冒充 1.0。"""
        _seed_raw(fake, "q", json.dumps(
            {"answer": "老答案", "citations": [], "debug_info": []}, ensure_ascii=False
        ))

        cached = redis_service.get_cached_answer("public", "q")

        assert cached["answer"] == "老答案"
        assert cached["rerank_score"] is None

    def test_explicit_null_score_reads_as_none(self, fake):
        _seed_raw(fake, "q", json.dumps({"answer": "a", "rerank_score": None}))

        assert redis_service.get_cached_answer("public", "q")["rerank_score"] is None

    def test_numeric_string_score_is_accepted(self, fake):
        """别的写入方（老版本 / 手工脚本）可能存字符串，能解析就用，别白丢一次命中。"""
        _seed_raw(fake, "q", json.dumps({"answer": "a", "rerank_score": "0.66"}))

        assert redis_service.get_cached_answer("public", "q")["rerank_score"] == 0.66

    @pytest.mark.parametrize("bad", ["abc", "", "不是数字", [1, 2], {"deep": 1}])
    def test_dirty_score_degrades_to_none(self, fake, bad):
        _seed_raw(fake, "q", json.dumps({"answer": "a", "rerank_score": bad}, ensure_ascii=False))

        assert redis_service.get_cached_answer("public", "q")["rerank_score"] is None

    def test_broken_json_is_a_miss_not_a_crash(self, fake):
        _seed_raw(fake, "q", "{not json")

        assert redis_service.get_cached_answer("public", "q") is None

    def test_non_dict_payload_is_a_miss(self, fake):
        """被写坏成列表的 key：当未命中，别让问答在 500 上翻车。"""
        _seed_raw(fake, "q", json.dumps(["not", "a", "dict"]))

        assert redis_service.get_cached_answer("public", "q") is None

    def test_unknown_key_is_a_miss(self, fake):
        assert redis_service.get_cached_answer("public", "从没缓存过") is None


class TestDonePayloadScore:
    """``done`` 载荷的 ``max_score``：缓存命中时必须回放缓存的原始分数。"""

    def test_cached_real_score_is_rounded_not_flattened(self):
        """0.6234567 → 0.6235（与缓存同精度），而不是被抹平成 1.0。"""
        payload = chat._done_payload(_chat_in(), "public", "q", "a", [], True, 0.6234567, False)

        assert payload["max_score"] == 0.6235

    def test_cache_and_done_payload_agree_on_the_same_score(self, fake):
        """模块 2 的本质断言：命中缓存显示的置信度 == 首次回答显示的置信度。

        两条路径各自做了一次 ``round(float(x), 4)``（缓存写入侧 / done 载荷侧），
        这里要求它们对同一个输入给出**完全一致**的结果，否则还是会显示两个数。
        """
        raw = 0.6234567
        redis_service.set_cached_answer("public", "q", "a", [], [], rerank_score=raw)

        cached = redis_service.get_cached_answer("public", "q")["rerank_score"]
        fresh = chat._done_payload(_chat_in(), "public", "q", "a", [], True, raw, False)

        assert cached == fresh["max_score"]

    def test_missing_score_stays_null(self):
        """老缓存没分数 → 载荷里是 null（前端据此不渲染置信度），而不是 1.0。"""
        payload = chat._done_payload(_chat_in(), "public", "q", "a", [], True, None, False)

        assert payload["max_score"] is None

    def test_fresh_answer_score_is_unchanged(self):
        payload = chat._done_payload(_chat_in(), "public", "q", "a", [], False, 0.42, False)

        assert payload["max_score"] == 0.42


class TestRouterWiring:
    """源码级回归防线（与 ``test_caching`` 里查 ``@st.cache_resource`` 同一手法）。"""

    def test_cache_hit_no_longer_hardcodes_one(self):
        src = (PROJECT_ROOT / "api" / "routers" / "chat.py").read_text(encoding="utf-8")

        assert "citations, True, 1.0, False" not in src, (
            "缓存命中路径又把置信度写死成 1.0 了（模块 2 修的就是它）"
        )

    def test_router_reads_and_writes_the_shared_score_key(self):
        src = (PROJECT_ROOT / "api" / "routers" / "chat.py").read_text(encoding="utf-8")

        assert "redis_service.CACHE_SCORE_KEY" in src, "读缓存分数必须走共享常量，别写字符串字面量"
        assert "rerank_score=max_score" in src, "写缓存时必须带上本次的真实分数"


#: 精确失效用例的固定原料：``UUID_A`` 是被删掉的那个文件，``UUID_B`` 是无关文件。
UUID_A = "uuid-file-a"
UUID_B = "uuid-file-b"


def _citations(*file_uuids):
    """造一份**合法**引用（FastAPI 入口的引用就是这个形状：带 ``file_uuid``）。"""
    return [
        {
            "source": f"{u}.pdf",
            "page": 1,
            "scope": "public",
            "file_uuid": u,
            "excerpt": "命中片段",
            "rerank_score": 0.9,
        }
        for u in file_uuids
    ]


class TestSourceFileUuidsAreRecorded:
    """``set_cached_answer`` 必须把"这条答案引用了哪些文件"与答案一起落盘。

    它是精确失效的**唯一依据**：没有它，删文件时只能把 ``KB_VERSION`` 抬一格
    （所有人的缓存一起归零），而那正是本次改造要避免的粗放做法。
    """

    def _control_hit(self, fake, file_uuid=UUID_A, question="对照组-引用了该文件"):
        """造一条**必须被删**的缓存，返回它的 key。

        专门配给那些「这条不该被删」的用例。没有对照组时它们对
        「把 ``invalidate_cache_by_file_uuid`` 换成空实现」完全无感 —— 空实现同样
        「什么也没删」，于是断言全绿（2026-10-14 用 no-op 插件复核：本组 4 条全绿，
        测试形同虚设）。有了对照组，「不该删的留着」和「该删的真删了」才同时被钉住。
        """
        redis_service.set_cached_answer(
            "public", question, "对照答案", _citations(file_uuid), []
        )
        return redis_service._build_cache_key("public", question, "all")

    def test_uuids_are_deduped_and_kept_in_order(self, fake):
        """一条答案可以引用多个文件；同一个文件出现在多条引用里只记一次。"""
        redis_service.set_cached_answer(
            "public", "q", "a", _citations(UUID_A, UUID_B, UUID_A), []
        )
        key = redis_service._build_cache_key("public", "q", "all")

        cached = redis_service.get_cached_answer("public", "q")

        assert cached[redis_service.SOURCE_FILE_UUIDS_KEY] == [UUID_A, UUID_B]
        # 闭环：记下来的 uuid 必须真能让这条缓存失效。只看字段内容是不够的 ——
        # 字段名写歪、抽取函数返回空列表、失效函数被改成空实现，那种断言都照样绿。
        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert key not in fake.strings

    def test_citations_without_uuid_contribute_nothing(self, fake):
        """没有 id 的引用（Streamlit 入口 / 老调用方）→ 空列表，**不许**塞 None 进去。

        塞了 None 的后果：将来一次"空 uuid 的删除请求"就可能误伤一片缓存。
        """
        redis_service.set_cached_answer(
            "public",
            "q-noid",
            "a",
            [{"source": "a.pdf", "page": 1}, {"file_uuid": None}, {"file_uuid": ""}],
            [],
        )
        noid = redis_service._build_cache_key("public", "q-noid", "all")
        control = self._control_hit(fake)

        cached = redis_service.get_cached_answer("public", "q-noid")

        assert cached[redis_service.SOURCE_FILE_UUIDS_KEY] == []
        # 对照组必须被删：否则「这条空列表的缓存没被删」在失效函数是空实现时也成立
        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert control not in fake.strings
        assert noid in fake.strings, "没有 uuid 的缓存不该被误删 —— 它只是无法按文件失效"

    def test_answer_without_citations_records_an_empty_list(self, fake):
        redis_service.set_cached_answer("public", "q-nocite", "a", [], [])
        nocite = redis_service._build_cache_key("public", "q-nocite", "all")
        control = self._control_hit(fake)

        assert (
            redis_service.get_cached_answer("public", "q-nocite")[
                redis_service.SOURCE_FILE_UUIDS_KEY
            ]
            == []
        )
        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert control not in fake.strings
        assert nocite in fake.strings, "没有任何引用的缓存不该被误删"

    def test_dirty_citation_does_not_break_the_write(self, fake):
        """一条坏引用不该让整次写缓存失败 —— 缓存只是加速手段，不该拖垮一次问答。"""
        redis_service.set_cached_answer(
            "public", "q-dirty", "a", ["不是 dict", _citations(UUID_A)[0]], []
        )
        key = redis_service._build_cache_key("public", "q-dirty", "all")

        cached = redis_service.get_cached_answer("public", "q-dirty")

        assert cached[redis_service.SOURCE_FILE_UUIDS_KEY] == [UUID_A]
        # 一条坏引用不该影响「被记下来的那个 uuid 仍然能用」（闭环断言，理由同上）
        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert key not in fake.strings


class TestInvalidateCacheByFileUuid:
    """删除文件 → 引用它的缓存必须消失（2026-10-14 报的事故）。

    事故现场：删除文件只清了 ES / Chroma / MySQL 记录，缓存里那条答案还在 →
    用户再问同样的问题就命中缓存 → 回答继续引用已删除的文件 → 点引用下载 **404**。

    需求原文的场景就是第一条用例：写 3 条缓存、其中 2 条引用 ``UUID_A``，
    调 ``invalidate_cache_by_file_uuid(UUID_A)`` 后，那 2 条被删、不引用 A 的 1 条保留。
    """

    def _seed(self, fake, question, *file_uuids, scope="public", filter_key="all"):
        """按"真实写入路径"造缓存，返回它的 key（便于断言 key 与 TTL 的去留）。"""
        redis_service.set_cached_answer(
            scope, question, f"答案-{question}", _citations(*file_uuids), [],
            filter_key=filter_key,
        )
        return redis_service._build_cache_key(scope, question, filter_key)

    def test_drops_only_the_entries_that_reference_the_file(self, fake):
        """★ 需求核心：引用 A 的两条被删、不引用 A 的那条**必须留着**。"""
        self._seed(fake, "q1", UUID_A)
        self._seed(fake, "q2", UUID_A, UUID_B)
        kept = self._seed(fake, "q3", UUID_B)

        removed = redis_service.invalidate_cache_by_file_uuid(UUID_A)

        assert removed == 2
        assert redis_service.get_cached_answer("public", "q1") is None
        assert redis_service.get_cached_answer("public", "q2") is None
        assert redis_service.get_cached_answer("public", "q3")["answer"] == "答案-q3"
        # 留下来的那条 TTL 也不能被顺手清掉（清掉 TTL 就等于让它永不过期）
        assert fake.ttls[kept] == 86400

    def test_deleted_key_loses_its_ttl_too(self, fake):
        key = self._seed(fake, "q1", UUID_A)

        redis_service.invalidate_cache_by_file_uuid(UUID_A)

        assert key not in fake.strings
        assert key not in fake.ttls

    def test_every_scope_and_filter_dimension_is_scanned(self, fake):
        """同一个文件可能被多个库、多个"只看这个文件"的问题引用 —— 一处都不能漏。

        缓存 key 含 scope 与 filter 两段，失效逻辑**不许**假设它们只有一种取值。
        """
        self._seed(fake, "q1", UUID_A, scope="public")
        self._seed(fake, "q2", UUID_A, scope="private:uuid-alice")
        self._seed(fake, "q3", UUID_A, scope="public+private:uuid-alice", filter_key=UUID_A)

        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 3

    def test_chat_history_is_untouched(self, fake):
        """对话历史不是"引用了文件"的缓存：它有自己的接口（DELETE /api/history）。"""
        fake.strings["chat_history:uuid-alice:public"] = "[]"
        hit = self._seed(fake, "q1", UUID_A)

        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert hit not in fake.strings, "对照组：引用了该文件的缓存必须被删"
        assert "chat_history:uuid-alice:public" in fake.strings

    def test_zero_hit_sends_no_delete_but_a_hit_deletes_exactly_one(self, fake):
        """命中判定与 DEL 次数都要对：没有引用 → 返回 0 且**一个 DEL 都不发**
        （真 Redis 上空 DEL 是命令错误）；一旦命中 → 只删那一条。
        """
        kept = self._seed(fake, "q1", UUID_B)

        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 0
        assert fake.delete_calls == 0
        assert kept in fake.strings

        # 同一个库里放一条对照组：命中时必须**真的删**。
        # 只有前半段的话，把失效函数换成空实现（同样"没删任何东西"）这条用例照样绿。
        hit = self._seed(fake, "q2", UUID_A)
        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert hit not in fake.strings
        assert kept in fake.strings, "只删命中的那条，别把无关缓存一起带走"
        assert fake.delete_calls == 1

    def test_empty_uuid_is_refused_but_a_valid_one_still_works(self, fake):
        """空 uuid 只可能是调用方的 bug：宁可什么都不删，也不许把缓存扫成"部分匹配"。"""
        entry = self._seed(fake, "q1", UUID_B)

        assert redis_service.invalidate_cache_by_file_uuid("") == 0
        assert redis_service.invalidate_cache_by_file_uuid(None) == 0
        assert redis_service.invalidate_cache_by_file_uuid("   ") == 0
        assert fake.delete_calls == 0
        assert entry in fake.strings, "空 uuid 只可能是调用方的 bug：宁可什么都不删"
        # 对照组：同一个库里换成正常 uuid，必须真的删掉。
        # 少了这一半，"空 uuid 没删任何东西"在空实现下也成立。
        assert redis_service.invalidate_cache_by_file_uuid(UUID_B) == 1
        assert entry not in fake.strings

    def test_legacy_entry_without_the_field_survives(self, fake):
        """本次改造前写入的缓存没有这个字段 → 没有依据，**不删**（靠 KB_VERSION 抬版本作废）。"""
        legacy = _seed_raw(
            fake, "legacy", json.dumps({"answer": "老答案", "citations": []}, ensure_ascii=False)
        )
        hit = self._seed(fake, "q1", UUID_A)

        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert hit not in fake.strings, "对照组：引用了该文件的缓存必须被删"
        assert legacy in fake.strings, "没有该字段的老缓存没有依据，不许删"
        assert redis_service.get_cached_answer("public", "legacy")["answer"] == "老答案"

    def test_broken_payload_is_kept_not_deleted(self, fake):
        """脏数据（非法 JSON）不许被顺手删掉：读不出来就没有依据说它引用了这个文件。"""
        broken = _seed_raw(fake, "broken", "{not json")
        hit = self._seed(fake, "q1", UUID_A)

        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert hit not in fake.strings, "对照组：引用了该文件的缓存必须被删"
        assert broken in fake.strings, "坏 payload 被一起删了 —— 少删一条只是慢，错删是丢数据"

    def test_string_field_does_not_half_match(self, fake):
        """``source_file_uuids`` 是**字符串**时不许按子串匹配（``"uuid-file-a" ⊂ "uuid-file-a-x"``）。

        这种误删不报错、不告警，只会让用户的缓存莫名其妙命中不了 —— 所以必须挡。
        """
        half = _seed_raw(
            fake, "half", json.dumps({"answer": "a", "source_file_uuids": f"{UUID_A}-suffix"})
        )
        hit = self._seed(fake, "q1", UUID_A)

        assert redis_service.invalidate_cache_by_file_uuid(UUID_A) == 1
        assert hit not in fake.strings, "字段是列表的那条必须被删（对照组）"
        assert half in fake.strings, "字符串字段被子串命中了 —— 这种误删不报错、不告警"

