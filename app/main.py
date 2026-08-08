"""
FMEA AI Agent – FastAPI 应用入口
版本 4.3.0 · 统一数据库引擎 · 生产就绪
"""
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

# ─────────────────── 日志配置（最先执行） ───────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fmea.main")

# ─────────────────── 数据库引擎（唯一入口） ───────────────────
# 导入 database 模块时，其末尾会自动 import app.models 完成表注册
try:
    from app.db.database import engine, Base
except ImportError:
    logger.critical("❌ 无法导入数据库模块，请检查 `app/db/database.py`", exc_info=True)
    sys.exit(1)

# ─────────────────── 路由导入 ───────────────────
try:
    from app.api.routes import router as project_router          # /projects
    from app.routers.analysis_tasks import router as analysis_router  # 分析任务
    from app.routers.fmea import router as fmea_router           # ✅ 新增：稳健 FMEA 路由
except ImportError as e:
    logger.critical("❌ 路由模块导入失败，请检查路径", exc_info=True)
    sys.exit(1)

# ─────────────────── 生命周期管理 ───────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时自动建表，关闭时释放资源（预留扩展）"""
    logger.info("🔄 初始化数据库表...")
    try:
        # Base.metadata 已包含所有模型（由 database.py 末尾 import 注册）
        Base.metadata.create_all(bind=engine)
        logger.info("✅ 数据库表已就绪")
    except Exception:
        logger.critical("❌ 数据库初始化失败，应用终止", exc_info=True)
        raise  # 阻止服务启动
    yield
    logger.info("🛑 应用关闭，清理资源")

# ─────────────────── FastAPI 实例 ───────────────────
app = FastAPI(
    title="特种设备缺陷解析与FMEA评估系统",
    description="多智能体协同复杂工作流工业级评估",
    version="4.3.0",
    lifespan=lifespan,
)

# ─────────────────── 注册路由 ───────────────────
app.include_router(project_router)
app.include_router(analysis_router)
app.include_router(fmea_router)          # ✅ 注册稳健 FMEA 路由

# ─────────────────── 根端点 ───────────────────
@app.get("/", tags=["系统信息"])
async def root():
    return {
        "service": "特种设备缺陷解析与FMEA评估",
        "version": "4.3.0",
        "docs": "/docs",
        "redoc": "/redoc",
    }

# ─────────────────── 直接运行入口 ───────────────────
if __name__ == "__main__":
    import uvicorn

    # 可选：预加载法规（若存在）
    try:
        from app.core.regulation_loader import load_regulation_folder
        load_regulation_folder()
        logger.info("📚 法规库预加载完成")
    except Exception:
        logger.warning("⚠ 法规预加载失败（不影响核心功能）", exc_info=True)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,          # 生产环境务必关闭
        log_level="info",
    )