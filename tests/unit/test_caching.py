"""``utils.caching`` 与"服务层不再依赖 ``st.cache_resource``"这一迁移不变量的单元测试（纯离线）。

这里锁定的契约：

1. **非 Streamlit 运行时必须真的缓存**（uvicorn / pytest / 脚本）：
   ``st.cache_resource`` 在 Streamlit 之外会退化成普通函数，于是
   Cross-Encoder(~80MB) / Docling 转换器变成"每个请求重建一次"——
   不报错、只是慢到不可用，属于最难发现的性能回归；
2. **Streamlit 运行时仍走 ``st.cache_resource``**，行为与改造前一致（零回归）；
3. 服务层不允许再出现裸的 ``st.cache_resource``（源码级检查，防止以后有人改回去）。

运行：``.venv\\Scripts\\python.exe -m pytest tests/unit -v``
"""
import pathlib
import re

from utils import caching

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestLruBranchOutsideStreamlit:
    """非 Streamlit 运行时：必须退化成 lru_cache 语义（同参数只构建一次）。"""

    def test_same_argument_builds_only_once(self, monkeypatch):
        monkeypatch.setattr(caching, "_streamlit_running", lambda: False)
        calls = []

        @caching.resource_cache
        def build(name):
            calls.append(name)
            return {"model": name}

        first = build("m1")
        second = build("m1")

        assert calls == ["m1"], "同一参数被重复构建，说明 API 运行时没有缓存（模型会被反复加载）"
        assert first is second

    def test_different_arguments_cached_separately(self, monkeypatch):
        monkeypatch.setattr(caching, "_streamlit_running", lambda: False)
        calls = []

        @caching.resource_cache
        def build(name):
            calls.append(name)
            return object()

        a = build("mini_lm")
        b = build("bge_base")

        assert a is not b
        assert calls == ["mini_lm", "bge_base"]


class TestStreamlitBranch:
    """Streamlit 运行时：必须走 st.cache_resource（而不是 lru），保住原有行为。"""

    def test_uses_streamlit_cache_when_runtime_exists(self, monkeypatch):
        monkeypatch.setattr(caching, "_streamlit_running", lambda: True)

        def raw(name):
            return name

        wrapped = caching.resource_cache(raw)

        assert callable(wrapped)
        # st.cache_resource 的返回值没有 lru_cache 的 cache_info；有就说明判断分支走错了
        assert not hasattr(wrapped, "cache_info")

    def test_streamlit_detection_is_consulted(self, monkeypatch):
        seen = {"count": 0}

        def fake_exists():
            seen["count"] += 1
            return False

        monkeypatch.setattr(caching, "_streamlit_running", fake_exists)
        caching.resource_cache(lambda name: name)

        assert seen["count"] == 1


class TestMigrationInvariant:
    """迁移不变量：服务层的贵资源必须走 resource_cache，否则 FastAPI 下不会缓存。"""

    def test_no_bare_st_cache_resource_in_services(self):
        """检查**装饰器用法**，而不是子串。

        注意：注释里合法地会提到 ``st.cache_resource``（要解释为什么不能再用它），
        所以这里必须按行精确匹配 ``@st.cache_resource`` 这种"真的在用"的写法，
        否则测试会被自己的说明文字误伤。
        """
        pattern = re.compile(r"^\s*@st\.cache_resource\b")
        offenders = []
        for path in sorted((PROJECT_ROOT / "services").glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if pattern.match(line):
                    offenders.append(f"{path.name}: {line.strip()}")
        assert offenders == [], (
            f"这些地方还在用裸 st.cache_resource：{offenders}——"
            "在 uvicorn(非 Streamlit 运行时) 下它不会缓存，模型/转换器会被反复重建"
        )

    def test_expensive_resources_use_resource_cache(self):
        for rel in ("services/retrieve_service.py", "services/pdf_service.py"):
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            assert "from utils.caching import resource_cache" in text, f"{rel} 未引入 resource_cache"
            assert "@resource_cache" in text, f"{rel} 未使用 resource_cache"


class TestRealCallSiteIsCached:
    """真实调用点确实带上了 lru 缓存（有 cache_info 即为 lru 分支，无需真加载模型）。"""

    def test_cross_encoder_getter_is_lru_cached(self):
        from services.retrieve_service import get_cross_encoder_model

        assert hasattr(get_cross_encoder_model, "cache_info"), (
            "get_cross_encoder_model 没有缓存信息，说明在非 Streamlit 环境下每次都会重建模型"
        )
