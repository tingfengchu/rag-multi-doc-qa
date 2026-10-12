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

    @property
    def chroma_root(self) -> str:
        return self.chroma_base_dir or self.base_dir

    model_config = SettingsConfigDict(
        env_file="rag_config.txt", 
        extra="ignore"
    )

settings = Settings()