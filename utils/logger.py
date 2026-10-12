import logging
import json
import sys

# LogRecord 自带属性：这些键不属于 extra，不能重复输出到 JSON 里。
# 注意 Python 3.12 起每个 LogRecord 都会带 taskName（asyncio 任务名），
# 3.13 起还会带 otel* 系列字段；不在这里过滤掉，日志里就会出现一堆
# "taskName": null 的噪音。
RESERVED_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "level", "time", "asctime", "taskName", "stacklevel",
    "otelSpanID", "otelTraceID", "otelTraceSampled", "otelServiceName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "level": record.levelname,
            "msg": record.getMessage(),
            "time": self.formatTime(record),
        }
        for k, v in record.__dict__.items():
            if k not in RESERVED_KEYS:
                payload[k] = v

        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps(
                {"level": record.levelname, "msg": "log serialization failed"},
                ensure_ascii=False,
            )


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("rag")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.addHandler(handler)
logger.propagate = False