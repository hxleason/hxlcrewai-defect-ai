"""
Alembic 迁移环境配置（生产级 · 自动适配 SQLite / 异步驱动）
============================================================
功能特性：
  1. 自动将异步数据库 URL（如 sqlite+aiosqlite）转换为同步 URL，无缝对接 Alembic。
  2. 自动导入 app.models 下所有模型，保证 Base.metadata 完整。
  3. SQLite 数据库强制启用「批处理模式」（render_as_batch=True），安全支持 ALTER 等复杂 DDL。
  4. 同时支持离线模式（生成 SQL 脚本）与在线模式（直接操作数据库）。

使用前请确保：
  - app.core.config.settings.DATABASE_URL 已正确配置。
  - app.models 的 __init__.py 中已导入所有 ORM 模型（否则 autogenerate 无法检测）。

作者：你的团队
最后更新：2026-08-11
"""

import os
import sys
from logging.config import fileConfig

# ---------------------------- 0. 基础路径配置 ----------------------------
# 将项目根目录加入 sys.path，确保后续导入 app 模块成功
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alembic import context
from sqlalchemy import create_engine, pool

# 导入项目核心配置与声明式 Base
from app.core.config import settings
from app.db.database import Base

# 自动导入所有模型（即使未显式使用，也必须保留）
import app.models  # noqa: F401

# ---------------------------- 1. 数据库 URL 同步化 ----------------------------
raw_url = settings.DATABASE_URL

# 异步驱动 → 同步驱动映射表，便于在开发/迁移场景间自动切换
SYNC_DRIVER_MAP = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql",
    "mysql+aiomysql": "mysql+pymysql",
}

for async_driver, sync_driver in SYNC_DRIVER_MAP.items():
    if async_driver in raw_url:
        raw_url = raw_url.replace(async_driver, sync_driver)
        print(f"[Alembic] 检测到异步驱动 '{async_driver}'，已自动切换为同步驱动 '{sync_driver}'。")
        break

# ---------------------------- 2. Alembic 配置初始化 ----------------------------
config = context.config
config.set_main_option("sqlalchemy.url", raw_url)

# 从 alembic.ini 加载日志配置（如存在）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 元数据目标：autogenerate 将基于此对比数据库结构
target_metadata = Base.metadata

# 是否为 SQLite（用于后续 batch 模式判断）
IS_SQLITE = raw_url.startswith("sqlite")


# ---------------------------- 3. 离线迁移模式 ----------------------------
def run_migrations_offline() -> None:
    """
    离线模式：生成 SQL 脚本，不实际连接数据库。
    适用于审查、审计或线上 DBA 手工执行。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite 同样启用 batch 模式，保证生成 SQL 的正确性
        render_as_batch=IS_SQLITE,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------- 4. 在线迁移模式 ----------------------------
def run_migrations_online() -> None:
    """
    在线模式：直接连接数据库并执行迁移。
    自动为 SQLite 设置 check_same_thread=False 并使用 batch 渲染。
    """
    connect_args = {}
    if IS_SQLITE:
        connect_args["check_same_thread"] = False

    # 使用 NullPool 保证每次连接都是全新的，避免事务残留
    engine = create_engine(
        raw_url,
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=IS_SQLITE,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


# ---------------------------- 5. 入口判断 ----------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()