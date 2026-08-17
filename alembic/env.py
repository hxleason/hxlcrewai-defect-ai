"""
Alembic 迁移环境配置（终极版 · 自动适配 SQLite）
==================================================
核心能力：
  1. 自动将异步数据库 URL（如 sqlite+aiosqlite）转换为同步 URL，无缝接入 Alembic。
  2. 自动导入 app.models 中所有模型，确保 Base.metadata 完整。
  3. 针对 SQLite 强制启用「批处理模式」（render_as_batch=True），安全执行 ALTER 等复杂 DDL。
  4. 同时支持离线模式（生成纯 SQL）与在线模式（直接操作数据库）。

使用前请确保：
  - app.core.config.settings.DATABASE_URL 已正确配置。
  - app.models 中已导入所有 ORM 模型（即使只是 import 放在 __init__.py 里）。
"""

import os
import sys
from logging.config import fileConfig

# -------------------- 路径与基础导入 --------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alembic import context
from sqlalchemy import engine_from_config, pool

# 项目定制导入
from app.core.config import settings      # 应用程序配置
from app.db.database import Base          # SQLAlchemy declarative base

# 确保所有模型被导入（即使 linter 提示未使用，也必须保留）
import app.models  # noqa: F401

# -------------------- 数据库 URL 同步化 --------------------
raw_url = settings.DATABASE_URL

# 异步驱动 → 同步驱动映射，方便开发时使用异步引擎，迁移时自动切换
SYNC_DRIVER_MAP = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql",
    "mysql+aiomysql": "mysql+pymysql",
}

for async_driver, sync_driver in SYNC_DRIVER_MAP.items():
    if async_driver in raw_url:
        raw_url = raw_url.replace(async_driver, sync_driver)
        break

# -------------------- Alembic 配置初始化 --------------------
config = context.config
config.set_main_option("sqlalchemy.url", raw_url)

# 使用 alembic.ini 中 [loggers] 等配置（若存在）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 元数据目标，autogenerate 将对比此 metadata 与数据库实际状态
target_metadata = Base.metadata

# -------------------- 迁移执行函数 --------------------
def run_migrations_offline() -> None:
    """离线模式：生成可直接执行的 SQL 脚本，不需要实际数据库连接。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接真实数据库并执行迁移。"""
    # 精准判断是否为 SQLite（URL 以 sqlite 开头）
    is_sqlite = raw_url.startswith("sqlite")

    # 连接参数：SQLite 需关闭同一线程检查
    connect_args = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False

    # 创建临时引擎（NullPool 确保每次迁移使用独立连接，避免残留事务）
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite 强制使用 batch 模式以支持 ALTER（必须！）
            render_as_batch=is_sqlite,
            # 若自动生成的迁移包含多余的列类型变化，可取消下方注释暂时屏蔽
            # compare_type=False,
        )

        with context.begin_transaction():
            context.run_migrations()


# -------------------- 入口 --------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()