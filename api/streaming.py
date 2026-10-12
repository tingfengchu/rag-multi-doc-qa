"""SSE（Server-Sent Events）帧编码 + 流式响应头（纯函数，可离线单测）。

为什么用 SSE 而不是 WebSocket：问答是**单向**流（服务端 → 客户端），
SSE 更简单、文本协议可直接 curl 调试；而 ``rag_service.generate_answer_stream()``
本身就是一个同步生成器，直接映射成 ``data:`` 帧即可，服务端不需要维护连接状态。

帧格式（一个事件 = 若干行 + 一个空行）::

    event: token
    data: {"text": "张"}

    event: done
    data: {"answer": "...", "citations": [{"source": "a.pdf", "page": 3}]}

⚠️ 两个容易踩的坑：

1. **正文里的换行必须走 JSON 转义**，不能直接拼原始文本——SSE 以换行分隔字段，
   多行答案被直接拼进去会把一帧拆成多帧，前端拼接出来的内容会错乱；
2. **nginx 反代必须关缓冲**（``X-Accel-Buffering: no``），否则流式会被整段缓存，
   表现为"转圈很久然后一次吐完"，看起来像"流式没生效"。
"""
import json
from typing import Any, Dict

#: 事件类型（对外契约，前端按 event 名分流）
EVENT_META = "meta"            # 前置信息：改写后的问题、是否命中缓存
EVENT_TOKEN = "token"          # 答案增量
EVENT_CITATIONS = "citations"  # 引用来源
EVENT_DEBUG = "debug"          # 调试面板用的块详情
EVENT_DONE = "done"            # 结束（携带完整答案，便于前端整段替换）
EVENT_ERROR = "error"          # 流中途出错（此时 HTTP 状态码已经发出去了，只能靠事件报错）

#: 流式响应头
SSE_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_event(event: str, data: Any) -> str:
    """编码一帧 SSE 文本（字典自动 JSON 序列化，中文不转义）。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_meta(**kwargs) -> str:
    return sse_event(EVENT_META, kwargs)


def sse_token(text: str) -> str:
    return sse_event(EVENT_TOKEN, {"text": text})


def sse_citations(citations: list) -> str:
    return sse_event(EVENT_CITATIONS, {"citations": citations})


def sse_debug(debug_info: list) -> str:
    return sse_event(EVENT_DEBUG, {"debug_info": debug_info})


def sse_done(result: Dict) -> str:
    return sse_event(EVENT_DONE, result)


def sse_error(msg: str) -> str:
    return sse_event(EVENT_ERROR, {"message": msg})
