"""FastAPI 应用入口（Phase 1）。

与 Streamlit 版**并存**，共用 ``services/`` / ``auth.py`` / ``utils/`` / ``config.py``，
所以两边行为天然一致，也不会出现"改了一边忘了另一边"。

启动（**``--workers 1`` 是硬要求**）::

    .venv\\Scripts\\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1

为什么必须单 worker：每个进程都会各自加载 embedding / Cross-Encoder(~80MB) / Docling，
多开几个 worker 就是几份模型常驻内存（GB 级），还会各自维护独立的向量库连接；
在个人机或小服务器上只会更慢更抖。并发交给 FastAPI 的线程池（同步端点）承担。
"""
from contextlib import asynccontextmanager
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from services import es_service
from utils.logger import logger

from .routers import admin, auth, chat, feedback, files, history

__version__ = "0.1.0"


def _make_std_streams_utf8() -> None:
    """让标准输出/错误**永远不会因为编码问题抛异常**（进程启动时调一次）。

    为什么必须做：Windows 控制台默认是 GBK(cp936)，任何含 emoji / 制表符的
    ``print`` 都会抛 ``UnicodeEncodeError``。而这类输出恰好都在**模型首次加载**
    路径上（transformers 的进度条、``services/retrieve_service.py`` 的 Cross-Encoder、
    ``services/pdf_service.py`` 的 Docling），异常一旦冒泡进 SSE 生成器就会被
    兜成 ``event: error``——用户看到的现象是"每次提问都失败"
    （报错发生在模型被缓存之前，所以每次都重来一遍）。

    关键取舍：只改 ``errors``，**不动 encoding**。
    强制成 UTF-8 会让本机 GBK 控制台把中文显示成乱码（本项目的日志几乎全是中文），
    得不偿失；保留控制台自带编码 + ``errors="replace"``，则中文照常可读，
    只有 emoji / 进度条那种奇特字符退化成 ``?``，而**绝不会**再毁掉一个请求。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


_make_std_streams_utf8()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动自检：确保 ES 索引存在。

    照搬 ``app.py:21-27`` 的教训——ES 没起来时**绝不能**让应用崩在启动阶段，
    否则用户只会看到一大片 traceback，完全不知道是哪个依赖出了问题。
    关键词检索臂不可用时，纯向量检索 + 生成的问答链路仍然可用。
    """
    try:
        es_service.init_es_index()
        app.state.es_ready, app.state.es_error = True, None
    except Exception as e:
        app.state.es_ready, app.state.es_error = False, str(e)
        logger.warning(
            "es_init_failed",
            extra={"err": str(e), "hint": "关键词检索臂不可用，纯向量检索仍可用"},
        )
    yield


app = FastAPI(
    title="多租户 RAG API",
    version=__version__,
    description="RAG 助手的 REST/SSE 接口（Phase 1：与 Streamlit 版并存，共用同一套业务层）",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

if settings.allowed_origins_list:
    # 开发期前端（Vite:5173）跨域用；生产由 nginx 同源反代时这段自然不生效
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,  # 会话走 Cookie，必须允许携带凭证
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(history.router, prefix="/api")
app.include_router(feedback.router, prefix="/api")
app.include_router(admin.router, prefix="/api")


@app.get("/api/health", tags=["基础"], summary="存活探针（免鉴权）")
def health():
    """给负载均衡/运维用的轻量探针：不碰 MySQL/Redis/LLM，避免探针本身把依赖压垮。"""
    return {
        "ok": True,
        "service": "rag-api",
        "version": __version__,
        "es_ready": getattr(app.state, "es_ready", False),
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常：日志里留全信息，响应只给一句中文，不向客户端泄露堆栈与内部细节。"""
    logger.error(
        "api_unhandled_error", extra={"path": request.url.path, "err": str(exc)}
    )
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请稍后重试"})
