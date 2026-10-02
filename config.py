import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # 字段名默认会自动匹配同名的环境变量（大写），所以 env="..." 可以省略了
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str
    mysql_db: str = "rag_app"

    zhipu_api_key: str
    openai_api_base: str

    base_dir: str = os.path.dirname(os.path.abspath(__file__))
    max_upload_mb: int = 20

    # V2 版本的配置方式
    model_config = SettingsConfigDict(
        env_file=".env", 
        extra="ignore"
    )

settings = Settings()