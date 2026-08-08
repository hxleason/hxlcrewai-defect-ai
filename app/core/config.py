"""
集中化配置中心
- 所有环境变量通过 .env 或系统环境注入
- 开发默认使用 SQLite / 本地 Redis，无需额外配置
- 敏感信息（API Key、数据库密码）严禁硬编码，必须由外部提供
"""
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# ─────────────── 1. 项目根 & .env 加载 ───────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOTENV = PROJECT_ROOT / ".env"
load_dotenv(DOTENV, override=False)

# 确保必要目录存在（避免首次运行时因缺目录报错）
(PROJECT_ROOT / "data").mkdir(exist_ok=True)
(Path(__file__).parent.parent / "data" / "standards").mkdir(parents=True, exist_ok=True)
(Path(__file__).parent.parent / "data" / "chroma_db").mkdir(parents=True, exist_ok=True)


# ─────────────── 2. 核心配置模型 ───────────────
class Settings(BaseSettings):
    """应用运行时配置，所有字段均可通过环境变量覆盖"""

    model_config = SettingsConfigDict(
        env_file=str(DOTENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ========== LLM 服务 ==========
    LLM_MODEL: str = "deepseek-v3"
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = "sk-f7f60a19b9154a6b9781f40c4759b6f4"            # ⚠️ 请在 .env 中设置，切勿提交到仓库！
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 4000

    # ========== 消息队列 & 缓存 ==========
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # ========== 数据库 ==========
    DATABASE_URL: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'app.db'}"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 40
    DB_ECHO: bool = False

    # ========== RAG 向量检索核心路径 ==========
    # 1. 本地模型绝对路径（worker 进程不会再因为工作目录不同而找不到）
    EMBEDDING_MODEL_PATH: str = str(
        PROJECT_ROOT / "models" / "bge-small-zh-v1.5"
    )
    # 2. HuggingFace 模型 ID（若本地路径不存在则回退到自动下载）
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-small-zh-v1.5"
    RETRIEVAL_TOP_K: int = 5
    AUTO_BUILD_INDEX: bool = False

    # 3. 标准文档存储目录（法规文件放这里）
    STANDARDS_FOLDER: str = str(
        Path(__file__).parent.parent / "data" / "standards"
    )
    # 4. Chroma 向量库持久化目录
    CHROMA_PERSIST_DIR: str = str(
        Path(__file__).parent.parent / "data" / "chroma_db"
    )

    # ========== 文档切分 ==========
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50


# 全局单例
settings = Settings()


# ─────────────── 3. 向后兼容的路径别名 ───────────────
# 旧代码若直接 import CONSTANT，可通过以下别名继续工作，
# 但推荐逐步迁移至 settings.XXX
DEFAULT_WALL_THICKNESS = None
RULES_PATH = PROJECT_ROOT / "app" / "core" / "rules.json"
REGULATIONS_DOC_PATH = PROJECT_ROOT / "app" / "rag" / "documents" / "regulations.txt"
FAISS_INDEX_PATH = PROJECT_ROOT / "app" / "rag" / "faiss_index"
LAW_FOLDER = PROJECT_ROOT / "regulations"

# 向后兼容：让旧模块依然能拿到 Path 对象
STANDARDS_FOLDER = Path(settings.STANDARDS_FOLDER)
CHROMA_PERSIST_DIR = Path(settings.CHROMA_PERSIST_DIR)