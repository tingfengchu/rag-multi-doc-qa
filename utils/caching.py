"""``st.cache_resource`` 的可迁移替代：同一个装饰器，两种运行时都只加载一次。

为什么必须有这个模块（不是"顺手优化"，是**迁移的硬前提**）：

``st.cache_resource`` 只在 Streamlit 运行时里生效；在 uvicorn / 脚本 / pytest 里
它会退化成**普通函数**（原注释见 ``services/retrieve_service.py``），
于是"加载一次"的语义直接消失——

* Cross-Encoder 重排模型 ~80MB；
* Docling 转换器（含版面/表格模型）更大。

退化的后果不是报错，而是**每个请求都重新加载一遍**：接口能通、日志正常，
只是慢到不可用——这种"沉默的性能崩塌"比报错更难发现。

``resource_cache`` 按运行时选实现：

* Streamlit 里 → ``st.cache_resource``（行为与改造前**逐字节一致**，零回归）；
* 其它运行时 → ``functools.lru_cache(maxsize=None)``（进程内单例，等价于"加载一次"）。

用法与 ``st.cache_resource`` 完全一致（必须只接**可哈希**参数，如模型名字符串）。
"""
import functools
import logging

logger = logging.getLogger("rag.caching")


def _streamlit_running() -> bool:
    """当前进程是否跑在 Streamlit 运行时里（单独成函数，便于单测替换）。"""
    try:
        from streamlit.runtime import exists

        return bool(exists())
    except Exception:  # streamlit 未安装 / 版本变化
        return False


def resource_cache(func):
    """进程级资源缓存：Streamlit 下走 ``st.cache_resource``，否则走 ``lru_cache``。"""
    if _streamlit_running():
        import streamlit as st

        return st.cache_resource(func)

    logger.debug("resource_cache_using_lru", extra={"func": func.__qualname__})
    return functools.lru_cache(maxsize=None)(func)
