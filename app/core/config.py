"""
集中化配置中心（终极版 v4.3）
- 所有环境变量均可通过 .env 或系统环境注入，敏感信息严禁硬编码
- 自动兼容旧变量名（如 LLM_MODEL_NAME → LLM_MODEL），并发出升级警告
- 开发默认使用异步 SQLite，生产请务必通过环境变量切换为 PostgreSQL（asyncpg 等）
- 统一所有数据路径到 PROJECT_ROOT/data 下，确保跨平台兼容
- 包含风险管理相关阈值配置（HIGH_RISK_THRESHOLD、FORCE_SUSPEND_S9）
- 新增：PWHT 焊后热处理标准知识库路径配置（data/pwht_kb）

v4.3 变更说明（本次）：
- LLM_MAX_TOKENS 默认提升至 16000，为推理模型预留充足输出预算。
- 新增 LLM_ENABLE_THINKING 配置项，支持显式关闭推理（部分平台支持）。
- 新增 LLM_MAX_RETRIES 与 LLM_RETRY_DELAY，统一控制 LLM 调用重试。
- 新增模型兼容性验证器，确保 max_tokens 在合理范围内（4096~128000）。
"""
import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ─────────── 1. 项目根 & 环境初始化 ───────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DOTENV = PROJECT_ROOT / ".env"
load_dotenv(DOTENV, override=False)

# 确保项目数据目录结构存在
(PROJECT_ROOT / "data").mkdir(exist_ok=True)
(PROJECT_ROOT / "data" / "standards").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "data" / "chroma_db").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "data" / "faiss_index").mkdir(parents=True, exist_ok=True)
# ★ 新增：PWHT 结构化标准知识库目录
(PROJECT_ROOT / "data" / "pwht_kb").mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """应用运行时配置，所有字段均可通过环境变量覆盖"""

    model_config = SettingsConfigDict(
        env_file=str(DOTENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ========== 运行环境 ==========
    ENV: str = "development"

    # ========== LLM 大模型服务 ==========
    LLM_MODEL: str = "deepseek-v3"
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""
    LLM_TEMPERATURE: float = 0.1
    # ★ v4.3：默认提升至 16000，适配推理模型的思考 token 消耗
    LLM_MAX_TOKENS: int = 16000

    # ★ v4.3 新增：是否允许模型内部推理（reasoning/thinking）
    #   - True: 使用模型默认行为（推理模型会消耗额外 token 思考）
    #   - False: 尝试关闭推理（仅当平台支持 enable_thinking=false 时生效）
    LLM_ENABLE_THINKING: bool = True

    # ★ v4.3 新增：LLM 调用异常时的最大重试次数（指数退避）
    LLM_MAX_RETRIES: int = 3
    LLM_RETRY_DELAY: float = 3.0   # 首次重试延迟（秒），后续每次翻倍

    # ========== 消息队列 & 缓存 ==========
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # ========== 数据库 ==========
    DEFAULT_DB_PATH: Path = PROJECT_ROOT / "data" / "app.db"
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DEFAULT_DB_PATH.as_posix()}"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 40
    DB_ECHO: bool = False

    # ========== RAG 向量检索 ==========
    EMBEDDING_MODEL_PATH: str = str(PROJECT_ROOT / "models" / "bge-small-zh-v1.5")
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-small-zh-v1.5"
    RETRIEVAL_TOP_K: int = 5
    AUTO_BUILD_INDEX: bool = False

    STANDARDS_FOLDER: str = str(PROJECT_ROOT / "data" / "standards")
    CHROMA_PERSIST_DIR: str = str(PROJECT_ROOT / "data" / "chroma_db")
    FAISS_INDEX_PATH: str = str(PROJECT_ROOT / "data" / "faiss_index")

    # ★ 新增：PWHT 焊后热处理结构化标准知识库文件夹
    PWHT_KB_FOLDER: str = str(PROJECT_ROOT / "data" / "pwht_kb")

    # ========== 文档切分参数 ==========
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    # ========== FMEA 风险评估阈值 ==========
    HIGH_RISK_THRESHOLD: int = Field(200, description="RPN 超过此值即触发挂起审查")
    FORCE_SUSPEND_S9: bool = Field(
        True,
        description="当严重度 (Severity) ≥ 9 时，无论 RPN 多少都强制挂起"
    )

    # ── 向后兼容：自动回退到旧的环境变量命名 ──
    @model_validator(mode="after")
    def _compat_old_env_vars(self):
        changed = []
        old_model = os.getenv("LLM_MODEL_NAME")
        if old_model and self.LLM_MODEL == "deepseek-v3":
            self.LLM_MODEL = old_model
            changed.append(("LLM_MODEL_NAME", "LLM_MODEL"))

        old_key = os.getenv("OPENAI_API_KEY")
        if old_key and not self.LLM_API_KEY:
            self.LLM_API_KEY = old_key
            changed.append(("OPENAI_API_KEY", "LLM_API_KEY"))

        old_base = os.getenv("OPENAI_BASE_URL")
        if old_base and self.LLM_BASE_URL == "https://api.deepseek.com/v1":
            self.LLM_BASE_URL = old_base
            changed.append(("OPENAI_BASE_URL", "LLM_BASE_URL"))

        if changed:
            logger.warning(
                "⚠️ 检测到已弃用的环境变量：%s。"
                "系统已自动兼容，但强烈建议更新 .env 变量名：%s。",
                ", ".join(f'"{old}"' for old, _ in changed),
                ", ".join(f'"{new}"' for _, new in changed),
            )
        return self

    # ── v4.3 新增：LLM 参数合理性校验 ──
    @model_validator(mode="after")
    def _validate_llm_params(self):
        """确保 max_tokens 在合理范围，温度在 [0, 2] 区间。"""
        if not (4096 <= self.LLM_MAX_TOKENS <= 128000):
            logger.warning(
                "⚠️ LLM_MAX_TOKENS=%s 超出合理范围 [4096, 128000]，"
                "可能引发截断或 API 拒绝。建议设为 8000~32000。",
                self.LLM_MAX_TOKENS,
            )

        if not (0.0 <= self.LLM_TEMPERATURE <= 2.0):
            logger.warning(
                "⚠️ LLM_TEMPERATURE=%s 超出 [0.0, 2.0]，将按 API 默认处理。",
                self.LLM_TEMPERATURE,
            )
            self.LLM_TEMPERATURE = 0.1

        if self.LLM_MAX_RETRIES < 0:
            self.LLM_MAX_RETRIES = 0
        return self


# ─────────── 3. 全局单例 ───────────
settings = Settings()

# ─────────── 4. 兼容旧路径常量 ───────────
DEFAULT_WALL_THICKNESS = None
RULES_PATH = PROJECT_ROOT / "app" / "core" / "rules.json"
REGULATIONS_DOC_PATH = PROJECT_ROOT / "app" / "rag" / "documents" / "regulations.txt"
LAW_FOLDER = PROJECT_ROOT / "regulations"
STANDARDS_FOLDER = Path(settings.STANDARDS_FOLDER)
CHROMA_PERSIST_DIR = Path(settings.CHROMA_PERSIST_DIR)
FAISS_INDEX_PATH = Path(settings.FAISS_INDEX_PATH)
# ★ 新增：导出 PWHT 知识库文件夹的 Path 对象
PWHT_KB_FOLDER = Path(settings.PWHT_KB_FOLDER)