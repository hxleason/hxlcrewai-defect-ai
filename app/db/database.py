"""
数据库引擎、会话、自动建表
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# ❕ 注意：下面的 settings 来自我们刚刚创建好的 app.core.config
from app.core.config import settings

# 如果还没有定义 Base，可以在这里临时声明（后面会统一到 models 中）
# 这里假设我们会在 app.models.db_models 里定义所有模型，并且它们都继承自同一个 Base
# 为避免循环引用，我们先创建一个临时的 Base，实际使用时再用那个 Base
Base = declarative_base()

# 针对 SQLite 的特殊处理
connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    echo=False,   # 开发时可设 True 看 SQL
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def create_tables():
    """
    根据模型创建所有表（需要提前 import 所有模型类）
    会在 FastAPI 启动时调用
    """
    # 这里必须从 db_models 导入所有模型，否则 Base.metadata 不会包含它们
    from app.models.db_models import Base as ModelBase
    ModelBase.metadata.create_all(bind=engine)

def get_db():
    """FastAPI 路由专用依赖：获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()