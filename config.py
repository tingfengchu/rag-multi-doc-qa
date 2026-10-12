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

    @property
    def allowed_origins_list(self) -> list:
        """把逗号分隔的白名单解析成列表（空串 → 空列表，避免 CORS 配置成 [" " ]）。"""
        return [o.strip() for o in (self.api_allowed_origins or "").split(",") if o.strip()]

    @property
    def chroma_root(self) -> str:
        return self.chroma_base_dir or self.base_dir

    model_config = SettingsConfigDict(
        env_file="rag_config.txt", 
        extra="ignore"
    )

settings = Settings()