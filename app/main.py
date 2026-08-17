"""
FMEA AI Agent – FastAPI 应用入口
版本 4.4.0 · 生产/开发环境分离 · 安全加固 · 法规自动加载 · Swagger 认证按钮

🐛 修改说明（v4.4.1）：
- 注入 OpenAPI Security Scheme，使 Swagger UI 显示 Authorize 🔒 按钮
- 用户点击按钮后输入 API Key，后续所有测试请求自动携带 X-API-Key 头
- 保持原有中间件逻辑不变，非文档路径仍强制要求认证
"""
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi   # ← 新增导入

# ── 日志配置（最先执行） ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fmea.main")

# ── 数据库引擎（唯一入口） ──
try:
    from app.db.database import engine, Base
except ImportError:
    logger.critical("❌ 无法导入数据库模块，请检查 `app/db/database.py`", exc_info=True)
    sys.exit(1)

# ── 配置 ──
try:
    from app.core.config import settings
except ImportError:
    logger.critical("❌ 无法导入配置模块 `app.core.config`", exc_info=True)
    sys.exit(1)

# ── 路由导入 ──
try:
    from app.api.routes import router as project_router
    from app.routers.analysis_tasks import router as analysis_router
    from app.routers.fmea import router as fmea_router
except ImportError:
    logger.critical("❌ 路由模块导入失败，请检查路径", exc_info=True)
    sys.exit(1)

# ── 安全中间件 ──
try:
    from app.auth import APIKeyMiddleware
except ImportError:
    logger.critical("❌ 安全中间件 `app.auth.APIKeyMiddleware` 导入失败", exc_info=True)
    sys.exit(1)


# ── 生命周期管理 ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时按需建表 & 加载法规，关闭时释放资源"""
    # ---- 启动阶段 ----
    logger.info("🚀 应用启动中，环境: %s", settings.ENV)

    # 1. 数据库初始化
    if settings.ENV == "development":
        logger.info("🔄 [开发模式] 自动创建数据库表...")
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("✅ 数据库表已就绪（通过 create_all）")
        except Exception:
            logger.critical("❌ 数据库初始化失败，应用终止", exc_info=True)
            raise
    else:
        logger.info(
            "🔒 [%s 模式] 数据库表由 Alembic 迁移管理，跳过自动建表",
            settings.ENV.upper(),
        )

    # 2. 法规预加载（非阻塞，失败只记录警告）
    try:
        from app.core.regulation_loader import load_regulation_folder

        load_regulation_folder()
        logger.info("📚 法规库预加载完成")
    except Exception:
        logger.warning("⚠️ 法规预加载失败（不影响核心功能）", exc_info=True)

    yield  # 应用运行中

    # ---- 关闭阶段 ----
    logger.info("🛑 应用关闭，清理资源")


# ── FastAPI 实例 ──
app = FastAPI(
    title="特种设备缺陷解析与FMEA评估系统",
    description="多智能体协同复杂工作流工业级评估",
    version="4.4.0",
    lifespan=lifespan,
)

# ═══════════════════════════════════════════════════════
#  👑 让 Swagger UI 显示 🔒 Authorize 按钮的核心代码
# ═══════════════════════════════════════════════════════
def custom_openapi():
    """注入 X-API-Key 认证方案到 OpenAPI 文档"""
    if app.openapi_schema:
        return app.openapi_schema

    # 生成基础 OpenAPI schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # 定义 Security Scheme（API Key，放在 Header 中）
    openapi_schema["components"]["securitySchemes"] = {
        "ApiKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "输入合法的 API Key（例如 20051207hxl）"
        }
    }

    # 🌐 全局应用此安全方案（所有端点都会显示小锁）
    openapi_schema["security"] = [{"ApiKeyHeader": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


# 替换默认的 openapi() 方法
app.openapi = custom_openapi
# ═══════════════════════════════════════════════════════

# ── 挂载安全中间件（必须在路由注册之前） ──
app.add_middleware(APIKeyMiddleware)

# ── 注册路由 ──
app.include_router(project_router)
app.include_router(analysis_router)
app.include_router(fmea_router)


# ── 根端点 ──
@app.get("/", tags=["系统信息"])
async def root():
    return {
        "service": "特种设备缺陷解析与FMEA评估",
        "version": "4.4.0",
        "docs": "/docs",
        "redoc": "/redoc",
    }


# ── 直接运行入口 ──
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )