import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # 字段名默认会自动匹配同名的环境变量（大写），所以 env="..." 可以省略了
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str
    mysql_db: str = "rag_app"
    es_host: str = "localhost"
    es_port: int = 9200
    redis_host: str = "localhost"
    redis_port: int = 6379
    zhipu_api_key: str
    openai_api_base: str

    base_dir: str = os.path.dirname(os.path.abspath(__file__))
    # 向量库根目录：留空 = 与 base_dir 相同（本地开发保持原行为）。
    # Docker 里设成 /app/chroma_store，这样只需挂载一个数据卷，
    # 所有 chroma_<scope> 子库都能持久化（否则私人库会随容器删除而丢失）。
    chroma_base_dir: str = ""
    max_upload_mb: int = 20

    # ---- 原始文件留存（文件管理 + 下载）----
    # 上传时把原件留一份到 ``<file_store_dir>/<file_uuid><ext>``，下载接口据此找回。
    # 留空 = ``<base_dir>/file_store``。
    #
    # 为什么必须新增这块存储：改造前上传的原件只落在 ``tempfile`` 里，
    # 解析完就被 ``pdf_service`` 删掉了（见 ``_process_with_docling`` 的 finally），
    # DB 里只剩元数据 —— "下载原文"当时**没有任何东西**可下。
    file_store_dir: str = ""
    # 历史原件兜底目录（逗号分隔，留空 = 项目根目录）。
    # 改造前入库的文件没有留存件，只能按**文件名精确匹配**去这些目录里找
    # （本机交付环境的原件就放在项目根目录，那 3 个公共库文档正是这样找回的）。
    file_legacy_search_dirs: str = ""

    # ---- REST API（FastAPI 迁移，Phase 1）----
    # 全部带默认值：老的 rag_config.txt / .env 不加任何一行也能照常跑起来。
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # 开发期前端（Vite）来源白名单，逗号分隔；生产由 nginx 同源反代，用不到 CORS。
    api_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # 会话有效期（秒），默认 8 小时；改密码会立刻吊销该用户全部会话（见 api/sessions.py）
    session_ttl_seconds: int = 8 * 3600
    session_cookie_name: str = "rag_session"
    # 生产走 HTTPS 时必须设 True，否则会话 Cookie 会在明文连接里裸奔
    session_cookie_secure: bool = False

    # ---- 注册开关（模块 2.2）----
    # 默认 **关闭**：自助注册默认开放等于"任何能打开登录页的人都能建号看公共库"，
    # 演示/交付环境不该是这个默认值。需要开放时在 rag_config.txt 里写
    # ``allow_register = true``（或设环境变量 ALLOW_REGISTER=true）。
    # ⚠️ 它只控制"登录页是否显示注册入口"，服务端**必须**同样校验
    # （见 api/routers/auth.py:register）——只藏前端入口是藏不住 API 的。
    allow_register: bool = False

    @property
    def allowed_origins_list(self) -> list:
        """把逗号分隔的白名单解析成列表（空串 → 空列表，避免 CORS 配置成 [" " ]）。"""
        return [o.strip() for o in (self.api_allowed_origins or "").split(",") if o.strip()]

    @property
    def chroma_root(self) -> str:
        return self.chroma_base_dir or self.base_dir

    @property
    def file_store_root(self) -> str:
        """原件留存目录（``file_store_dir`` 留空 → ``<base_dir>/file_store``）。

        做成属性而不是在 ``services/file_store.py`` 里直接读字段：
        和 ``chroma_root`` 同一套写法，Docker 里挂一个数据卷就能持久化。
        """
        return self.file_store_dir or os.path.join(self.base_dir, "file_store")

    @property
    def file_legacy_dir_list(self) -> list:
        """历史原件兜底目录列表（留空 → 只找项目根目录）。

        与 ``allowed_origins_list`` 同样是"逗号分隔字符串 → 列表"，
        保持解析规则一致；空串不能变成 ``[""]``（那会让 ``os.path.join("", name)``
        退化成相对当前工作目录查找，等于把兜底目录交给"进程从哪启动"决定）。
        """
        raw = self.file_legacy_search_dirs or self.base_dir
        return [d.strip() for d in raw.split(",") if d.strip()]

    model_config = SettingsConfigDict(
        env_file="rag_config.txt", 
        extra="ignore"
    )

settings = Settings()