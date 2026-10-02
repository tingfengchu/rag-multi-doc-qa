import logging, json, sys

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "level": record.levelname,
            "msg": record.getMessage(),
            "time": self.formatTime(record),
        }
        for k, v in record.__dict__.items():
            if k not in ("name","msg","args","levelname","levelno","pathname",
                         "filename","module","exc_info","exc_text","stack_info",
                         "lineno","funcName","created","msecs","relativeCreated",
                         "thread","threadName","processName","process","message"):
                payload[k] = v
        return json.dumps(payload, ensure_ascii=False)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("rag")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.addHandler(handler)
logger.propagate = False