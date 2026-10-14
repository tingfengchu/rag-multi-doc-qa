"""E2E：删除文件 → 引用它的语义缓存失效 → 再问同样的问题必须**重新检索**。

为什么非得是 E2E（各个环节的单测已经有了）：用户报的事故发生在**串起来**的路径上 ——
删除文件（ES / 向量 / 记录都清干净）→ 用户再问同样的问题 → 命中缓存 → 回答继续引用
已删除的文件 → 点引用下载 **404**。三个环节各自看都对，串起来才是错的。

用例走的正是这条路径（全程真实接口 + 真实服务）：

1. 临时账号 → 在自己的私人库里上传**两份** md（一份含唯一代号 A，一份无关 B）；
2. 提问（问 A 的代号）→ 首次回答 ``cached=false``，引用里带着 A 的 uuid；
3. 再问同一个问题 → **命中缓存** ``cached=true`` —— 事故前提成立；
4. 走真实 ``DELETE /api/files/{uuid}`` 删掉 A；
5. Redis 里**不再有任何** ``cache:*`` 条目引用 A（精确失效的直接证据）；
6. 再问同一个问题 → ``cached=false``（重新走检索），引用里不再有 A；
   同时确认 A 的下载接口如实回 404（老路径撞上的就是这个 404）。

为什么留一份 B 不删：私人库清空后问答接口会在"空库"分支直接返回 error
（见 ``api/routers/chat.py`` 的空库剔除），那就测不到"重新检索"了。

为什么用"临时账号 + 私人库"：E2E 必须能自己收尾。删公共库的真文件不可逆；而私人库的一切
（Chroma 集合 / ES 块 / MySQL 记录 / 留存原件 / 缓存 / 历史 / 账号）都挂在临时账号名下，
``finally`` 里一并清掉（账号没有删除接口，按 ``tests/integration/test_api_auth.py`` 的
老办法物理删）。

需要真实 MySQL / Redis / ES / Chroma + 真实 LLM（``pytest.mark.integration``）。
服务不可用时**跳过**而不是报错：纯离线跑单测的机器不该被这条用例拖红。

运行：``.venv\\Scripts\\python.exe -m pytest tests/integration -m integration -v -s``
"""
import json
import os
import shutil
import uuid

# 与 tests/integration/test_rag_integration.py 一致：模型/镜像下载走 hf-mirror
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from db import get_conn
from services import redis_service

pytestmark = pytest.mark.integration

PASSWORD = "E2eCache_123!"


def _split_events(text):
    """把 SSE 响应体解成 ``[(event, data), ...]``（帧格式由 tests/unit/test_sse.py 锁定）。"""
    out = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def _ask(client, question, scope):
    """问一次，返回 ``(done 载荷, citations 列表)``。"""
    resp = client.post(
        "/api/chat/stream",
        json={"question": question, "scope": scope, "enable_rerank": False},
    )
    assert resp.status_code == 200, f"问答接口没走通：{resp.status_code} {resp.text}"
    events = _split_events(resp.text)
    done = [data for event, data in events if event == "done"]
    assert done, f"没有收到 done 事件：{events}\n{resp.text[:400]}"
    citations = [data for event, data in events if event == "citations"]
    return done[-1], (citations[0]["citations"] if citations else [])


def _cited_uuids(citations):
    return [c.get("file_uuid") for c in citations]


def _cache_keys_citing(file_uuid):
    """真 Redis 里还有哪些 ``cache:*`` 引用了这个文件（精确失效的直接证据）。"""
    hits = []
    for key in redis_service.r.scan_iter("cache:*"):
        raw = redis_service.r.get(key)
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            continue
        if isinstance(payload, dict) and file_uuid in (
            payload.get(redis_service.SOURCE_FILE_UUIDS_KEY) or []
        ):
            hits.append(key)
    return hits


def test_deleting_a_file_drops_the_cached_answers_that_cited_it(monkeypatch):
    """报告的事故：删文件 → 再问同样的问题 → 仍命中缓存 → 点引用下载 404。"""
    reason = _unavailable_reason()
    if reason:
        pytest.skip(reason)
    # 注册开关从模块 2.2 起默认关闭（做法同 tests/integration/test_api_auth.py）
    monkeypatch.setattr(settings, "allow_register", True)

    token = uuid.uuid4().hex[:8]
    code = f"ZEPHYR-{token.upper()}"
    question = f"内部项目代号 {code} 对应的是什么项目？"
    doc_a = (
        f"# 缓存失效测试文档 {token}\n\n"
        f"本文档记录：内部项目代号 {code} 对应的是语义缓存失效的验证项目。\n"
    ).encode("utf-8")
    doc_b = (
        f"# 无关文档 {token}\n\n"
        f"本文档只用来保证私人库非空，内容与任何项目代号无关，编号 {token}。\n"
    ).encode("utf-8")
    username = f"e2e_cache_{token}"

    scope = None
    user_uuid = None
    uploaded = []

    try:
        with TestClient(app) as client:
            reg = client.post(
                "/api/auth/register", json={"username": username, "password": PASSWORD}
            )
            assert reg.status_code == 201, reg.text
            user_uuid = reg.json()["user_uuid"]
            scope = f"private:{user_uuid}"
            login = client.post(
                "/api/auth/login", json={"username": username, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text

            try:
                for name, blob in (
                    (f"cache-e2e-a-{token}.md", doc_a),
                    (f"cache-e2e-b-{token}.md", doc_b),
                ):
                    up = client.post(
                        "/api/files/upload",
                        data={"scope": scope},
                        files={"file": (name, blob, "text/markdown")},
                    )
                    assert up.status_code == 200, up.text
                    uploaded.append(up.json()["file_uuid"])
                target, decoy = uploaded[0], uploaded[1]

                first, first_cites = _ask(client, question, scope)
                if first["refused"] or not _cache_keys_citing(target):
                    pytest.skip(
                        "前提不成立：首次回答没有写入引用目标文件的缓存"
                        f"（refused={first['refused']}，答案开头：{first['answer'][:80]!r}）"
                    )
                assert first["cached"] is False, "第一次提问不该命中缓存"
                assert target in _cited_uuids(first_cites), (
                    "首次回答必须引用刚上传的文件，否则后面的失效断言没有意义"
                )

                # ★ 事故前提：同一个问题第二次提问命中缓存（这条缓存带着目标文件的 uuid）
                second, _ = _ask(client, question, scope)
                assert second["cached"] is True, "第二次必须命中缓存 —— 这正是事故的状态"

                # ★ 删除文件：缓存失效就发生在真实删除接口这条路径里
                deleted = client.delete(f"/api/files/{target}", params={"scope": scope})
                assert deleted.status_code == 200, deleted.text

                assert _cache_keys_citing(target) == [], (
                    "删除文件后，引用它的缓存必须一条都不剩（否则用户点引用照样 404）"
                )

                # ★ 再问同一个问题：必须重新走检索
                third, third_cites = _ask(client, question, scope)
                assert third["cached"] is False, "引用文件已删除，必须重新检索而不是回放旧缓存"
                assert target not in _cited_uuids(third_cites), (
                    "回答里不许再出现已删除的文件（点它的引用就是 404）"
                )
                assert decoy in _cited_uuids(third_cites), (
                    "重新检索应命中还活着的那份文档（说明确实去检索了，而不是空转）"
                )
                # 老路径撞上的那个 404：确认它确实存在（也就是"引用已删文件"的后果）
                gone = client.get(f"/api/files/{target}/download", params={"scope": scope})
                assert gone.status_code == 404, gone.text
            finally:
                # HTTP 收尾：上传的文件全删掉（已经删过的回 404，无所谓）
                for file_uuid in uploaded:
                    client.delete(f"/api/files/{file_uuid}", params={"scope": scope})
    finally:
        # 环境收尾：缓存 / 历史 / 账号 / 私人库的 Chroma 目录
        if scope and user_uuid:
            for file_uuid in [*uploaded, ""]:
                for key in _cache_keys_citing(file_uuid):
                    redis_service.r.delete(key)
            # 本次问答写下的缓存（key 由 scope + 问题哈希决定，问题无代词 → 不改写）
            redis_service.r.delete(redis_service._build_cache_key(scope, question, "all"))
            redis_service.clear_history(user_uuid, scope)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    # 检索流水（模块 6）也要清：那些行带着临时账号与测试文件的 uuid，
                    # 留着会混进运营看板的"被检索最多的文档"里（旁路数据，没人会去清）
                    cur.execute("DELETE FROM retrieval_log WHERE user_uuid = %s", (user_uuid,))
                    cur.execute("DELETE FROM users WHERE user_uuid = %s", (user_uuid,))
            # 每跑一次集成测试就多一个 chroma_private_* 目录，没人会想到去清 ——
            # best-effort 删掉（Chroma 可能还握着文件句柄，失败就算了，不影响结论）
            shutil.rmtree(
                os.path.join(settings.chroma_root, f"chroma_private_{user_uuid}"),
                ignore_errors=True,
            )


def _unavailable_reason():
    """四个服务有一个不可用就跳过（返回空串表示都可用）。"""
    try:
        if not redis_service.r.ping():
            return "Redis 不可用"
    except Exception as exc:  # noqa: BLE001
        return f"Redis 不可用：{exc}"
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                # 结果必须取走：mysql-connector 在 cursor.close() 时会对"仍有未读结果"的
                # 游标抛 InternalError("Unread result found")，那会被这里误判成"MySQL 不可用"
                # ——第一次实跑就是这么跳过整条用例的（探活自己把服务判死了）。
                cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return f"MySQL 不可用：{exc}"
    try:
        from services import es_service

        if not es_service.es.ping():
            return "ES 不可用"
    except Exception as exc:  # noqa: BLE001
        return f"ES 不可用：{exc}"
    return ""
