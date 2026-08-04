import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # ==================== Pydantic 配置（忽略 .env 中多余字段） ====================
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"          # ★ 如果 .env 里有 Settings 未定义的字段，直接忽略，不报错
    )

    # ==================== LLM 配置（原样保留） ====================
    LLM_MODEL: str = "deepseek-v4-pro"
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 4000

    # ==================== 数据库与 Celery 配置 ====================
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://user:password@localhost:3306/fmea_db"
    )
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

    # ==================== 数据库连接池（新增） ====================
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 40

settings = Settings()