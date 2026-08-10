"""
集中化配置中心（终极版 v2.0）
- 所有环境变量通过 .env 或系统环境注入，敏感信息严禁硬编码
- 自动兼容旧变量名（如 LLM_MODEL_NAME → LLM_MODEL），并发出升级警告
- 开发默认使用 SQLite / 本地 Redis，无需额外配置
"""
import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ─────────────── 1. 项目根 & .env 加载 ───────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOTENV = PROJECT_ROOT / ".env"
load_dotenv(DOTENV, override=False)  # 环境变量优先于 .env 文件（标准实践）

# 确保必要目录存在（避免首次运行时因缺目录报错）
(PROJECT_ROOT / "data").mkdir(exist_ok=True)
(Path(__file__).parent.parent / "data" / "standards").mkdir(parents=True, exist_ok=True)
(Path(__file__).parent.parent / "data" / "chroma_db").mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)

# ─────────────── 2. 核心配置模型 ───────────────
class Settings(BaseSettings):
    """应用运行时配置，所有字段均可通过环境变量覆盖"""

    model_config = SettingsConfigDict(
        env_file=str(DOTENV),       # 自动读取 .env 文件
        env_file_encoding="utf-8",
        extra="ignore",             # 忽略未定义的额外环境变量
    )

    # ========== LLM 大模型服务 ==========
    LLM_MODEL: str = "deepseek-v3"
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""            # 生产环境必须提供（通过 .env 或系统环境变量）
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 4000

    # ========== 消息队列 & 缓存（Celery/Redis） ==========
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # ========== 数据库 ==========
    DATABASE_URL: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'app.db'}"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 40
    DB_ECHO: bool = False

    # ========== RAG 向量检索核心路径 ==========
    # 1. 本地嵌入模型路径（Worker 进程使用）
    EMBEDDING_MODEL_PATH: str = str(
        PROJECT_ROOT / "models" / "bge-small-zh-v1.5"
    )
    # 2. HuggingFace 模型 ID（仅当本地模型不可用时自动下载）
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-small-zh-v1.5"
    RETRIEVAL_TOP_K: int = 5
    AUTO_BUILD_INDEX: bool = False

    # 3. 法规标准文档目录
    STANDARDS_FOLDER: str = str(
        Path(__file__).parent.parent / "data" / "standards"
    )
    # 4. Chroma 向量库持久化目录
    CHROMA_PERSIST_DIR: str = str(
        Path(__file__).parent.parent / "data" / "chroma_db"
    )

    # ========== 文档切分参数 ==========
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    # ── 向后兼容：自动回退到旧的环境变量命名 ──
    @model_validator(mode="after")
    def _compat_old_env_vars(self):
        """
        在 Pydantic 模型构造完成后执行：
        - 若新变量未显式设置，但旧变量存在，则自动回退到旧值
        - 记录警告日志，提醒开发者升级环境变量名
        旧变量映射规则：
            LLM_MODEL_NAME   → LLM_MODEL
            OPENAI_API_KEY   → LLM_API_KEY
            OPENAI_BASE_URL  → LLM_BASE_URL
        """
        changed = []

        # 模型名称
        old_model = os.getenv("LLM_MODEL_NAME")
        if old_model and self.LLM_MODEL == "deepseek-v3":  # 默认值未被覆盖
            self.LLM_MODEL = old_model
            changed.append(("LLM_MODEL_NAME", "LLM_MODEL"))

        # API Key
        old_key = os.getenv("OPENAI_API_KEY")
        if old_key and not self.LLM_API_KEY:                # 新 Key 为空
            self.LLM_API_KEY = old_key
            changed.append(("OPENAI_API_KEY", "LLM_API_KEY"))

        # Base URL
        old_base = os.getenv("OPENAI_BASE_URL")
        if old_base and self.LLM_BASE_URL == "https://api.deepseek.com/v1":  # 默认值
            self.LLM_BASE_URL = old_base
            changed.append(("OPENAI_BASE_URL", "LLM_BASE_URL"))

        if changed:
            logger.warning(
                "⚠️ 检测到已弃用的环境变量：%s。"
                "系统已自动兼容，但强烈建议将 .env 文件中的变量名更新为标准名称：%s。"
                "请参考 .env.example 进行修改。",
                ", ".join(f'"{old}"' for old, _ in changed),
                ", ".join(f'"{new}"' for _, new in changed),
            )

        return self

# 全局单例
settings = Settings()


# ─────────────── 3. 向后兼容的路径别名（旧模块仍然可用） ───────────────
DEFAULT_WALL_THICKNESS = None
RULES_PATH = PROJECT_ROOT / "app" / "core" / "rules.json"
REGULATIONS_DOC_PATH = PROJECT_ROOT / "app" / "rag" / "documents" / "regulations.txt"
FAISS_INDEX_PATH = PROJECT_ROOT / "app" / "rag" / "faiss_index"
LAW_FOLDER = PROJECT_ROOT / "regulations"

# 将配置中的字符串路径转为 Path 对象，方便旧代码直接使用
STANDARDS_FOLDER = Path(settings.STANDARDS_FOLDER)
CHROMA_PERSIST_DIR = Path(settings.CHROMA_PERSIST_DIR)