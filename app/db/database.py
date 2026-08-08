"""
项目全局唯一的数据库引擎、会话工厂、ORM 基类。
所有其他模块（路由、CRUD、Celery 任务）均通过本文件导入：
    from app.db.database import Base, engine, SessionLocal, get_db

配置来源: app.core.config.settings（基于 pydantic-settings，自动加载 .env）
支持数据库：SQLite（开发默认） / MySQL / PostgreSQL（生产）
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings

# ──────────────── 全局 ORM 基类 ────────────────
Base = declarative_base()

# ──────────────── 读取配置 ────────────────
DATABASE_URL = settings.DATABASE_URL

# ──────────────── 构建引擎参数（智能适配数据库类型） ────────────────
connect_args = {}
engine_kwargs = {}

if DATABASE_URL.startswith("sqlite"):
    # SQLite 必须允许多线程访问（FastAPI 默认多线程）
    connect_args["check_same_thread"] = False
    # SQLite 不支持连接池，显式移除池化参数（避免警告）
    engine_kwargs.pop("pool_size", None)
    engine_kwargs.pop("max_overflow", None)
else:
    # 服务器数据库（MySQL/PostgreSQL）启用连接池
    engine_kwargs["pool_size"] = getattr(settings, "DB_POOL_SIZE", 20)
    engine_kwargs["max_overflow"] = getattr(settings, "DB_MAX_OVERFLOW", 0)

# 生成引擎（echo 建议通过配置控制，方便开发调试）
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=getattr(settings, "DB_ECHO", False),
    **engine_kwargs,
)

# ──────────────── 会话工厂 ────────────────
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# ──────────────── FastAPI 依赖注入 ────────────────
def get_db():
    """每个请求获取一个数据库会话，结束后自动安全关闭"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ──────────────── 自动注册所有 ORM 模型 ────────────────
# 必须放在文件末尾，避免循环引用；所有模型类必须继承本文件的 Base
import app.models  # noqa: E402, F401