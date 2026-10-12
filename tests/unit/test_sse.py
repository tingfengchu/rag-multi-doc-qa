"""``api.streaming`` 的单元测试（纯函数、离线）。

锁定的契约：

1. 帧格式严格是 SSE（``event:`` 行 + ``data:`` 行 + **空行**结束），前端才能正确切分；
2. **多行内容必须被 JSON 转义**，不能让正文里的换行把一帧拆成多帧——
   这是 SSE 集成里最常见、也最难查的 bug（表现是"流式答案拼接错乱"）；
3. 中文不转义成 ``\\uXXXX``（可读性与体积）；
4. 反代必须禁缓冲（``X-Accel-Buffering: no``），否则"流式"会退化成"转圈很久一次吐完"；
5. 事件名是对外契约：前端按这些名字分流，改名等于破坏前端。
"""
import json

from api import streaming


def _parse(frame: str):
    """把一帧解回 ``(event, data)``，顺便验帧结构。"""
    assert frame.endswith("\n\n"), "SSE 帧必须以空行结束"
    lines = frame[:-2].split("\n")
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0][len("event: "):], json.loads(lines[1][len("data: "):])


class TestFrameFormat:
    def test_single_line_text_frame(self):
        event, data = _parse(streaming.sse_token("你好"))
        assert event == streaming.EVENT_TOKEN
        assert data == {"text": "你好"}

    def test_multiline_content_stays_one_frame(self):
        text = "第一行\n第二行\n\n第四行"
        frame = streaming.sse_token(text)

        # 3 个换行全在 JSON 里，帧结构本身只有 event/data/空行
        assert frame.count("\n") == 3
        event, data = _parse(frame)
        assert data["text"] == text

    def test_chinese_is_not_escaped(self):
        frame = streaming.sse_token("中文答案")

        assert "中文答案" in frame
        assert "\\u4e2d" not in frame

    def test_citations_frame(self):
        event, data = _parse(streaming.sse_citations([{"source": "简历.pdf", "page": 3}]))
        assert event == streaming.EVENT_CITATIONS
        assert data["citations"][0]["source"] == "简历.pdf"

    def test_debug_frame(self):
        event, data = _parse(streaming.sse_debug([{"length": 120, "is_parent": True}]))
        assert event == streaming.EVENT_DEBUG
        assert data["debug_info"][0]["is_parent"] is True

    def test_meta_frame_kwargs(self):
        event, data = _parse(streaming.sse_meta(scope="public", cached=True))
        assert event == streaming.EVENT_META
        assert data == {"scope": "public", "cached": True}

    def test_done_and_error_frames(self):
        _, done = _parse(streaming.sse_done({"answer": "ok", "cached": False}))
        _, err = _parse(streaming.sse_error("处理失败：模型超时"))
        assert done["answer"] == "ok"
        assert err == {"message": "处理失败：模型超时"}


class TestExternalContract:
    def test_event_names_are_stable(self):
        """前端 switch 这些字符串：改名 = 破坏前端，必须显式改测试才算有意为之。"""
        assert (
            streaming.EVENT_META,
            streaming.EVENT_TOKEN,
            streaming.EVENT_CITATIONS,
            streaming.EVENT_DEBUG,
            streaming.EVENT_DONE,
            streaming.EVENT_ERROR,
        ) == ("meta", "token", "citations", "debug", "done", "error")

    def test_headers_disable_proxy_buffering(self):
        assert streaming.SSE_HEADERS["X-Accel-Buffering"] == "no"
        assert streaming.SSE_HEADERS["Cache-Control"] == "no-cache"

    def test_string_payload_is_passed_through(self):
        """data 传裸字符串时不额外加引号（保持可读的调试输出）。"""
        assert streaming.sse_event("ping", "raw") == "event: ping\ndata: raw\n\n"
