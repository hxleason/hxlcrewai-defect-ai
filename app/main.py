"""
FMEA AI Agent – FastAPI 应用入口
版本 4.5.0 · 生产/开发环境分离 · 安全加固 · 法规自动加载 · Swagger 认证按钮

🐛 修改说明（v4.4.1，保留）：
- 注入 OpenAPI Security Scheme，使 Swagger UI 显示 Authorize 🔒 按钮
- 用户点击按钮后输入 API Key，后续所有测试请求自动携带 X-API-Key 头
- 保持原有中间件逻辑不变，非文档路径仍强制要求认证

🐛 修改说明（v4.5.0，本次 · Phase 1 审核门与一致性配套）：
- 新增 /health 健康检查端点：返回 DB 与 Redis 连通状态，
  full_evaluation_v2 的幂等锁依赖 Redis，运维可据此快速定位降级原因
- 启动阶段 fail-fast 校验新增核心模块（app/core/review_gate.py、
  app/core/defect_utils.py），缺失时直接终止启动，避免半损坏状态运行
- 启动阶段增加 Redis 连通性检查（失败仅告警，不阻断启动）
- 新增全局异常处理器：FMEABaseException 结构化映射 + 兜底 Exception，
  保证任何路由抛出的业务异常都返回统一 JSON 错误结构
"""
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi   # ← 新增导入
from fastapi.responses import JSONResponse
from sqlalchemy import text

# ── 日志配置（最先执行） ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fmea.main")

# ── 数据库引擎（唯一入口） ──
try:
    from app.db.database import engine, Base, SessionLocal
except ImportError:
    logger.critical("❌ 无法导入数据库模块，请检查 `app/db/database.py`", exc_info=True)
    sys.exit(1)

# ── 配置 ──
try:
    from app.core.config import settings
except ImportError:
    logger.critical("❌ 无法导入配置模块 `app.core.config`", exc_info=True)
    sys.exit(1)

# ── v4.5.0 核心一致性模块校验（fail-fast）──
try:
    from app.core.review_gate import needs_review  # noqa: F401 统一审核闸门
    from app.core.defect_utils import (            # noqa: F401 缺陷工具唯一实现
        propagate_wall_thickness,
        assign_temp_defect_ids,
    )
except ImportError:
    logger.critical(
        "❌ Phase 1 核心模块缺失：请确认 app/core/review_gate.py 与 "
        "app/core/defect_utils.py 已创建", exc_info=True,
    )
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

# ── 业务异常（用于全局异常处理器）──
try:
    from app.core.exceptions import FMEABaseException
except ImportError:
    logger.critical("❌ 异常模块 `app.core.exceptions` 导入失败", exc_info=True)
    sys.exit(1)


# ── 生命周期管理 ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时按需建表 & 加载法规 & 检查依赖，关闭时释放资源"""
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

    # 3. Redis 连通性检查（v4.5.0 新增）
    #    full_evaluation_v2 的幂等锁依赖 Redis；失败仅告警，不阻断启动。
    try:
        from app.tasks.analysis import redis_client

        redis_client.ping()
        logger.info("🧱 Redis 连接正常（幂等锁可用）")
    except Exception:
        logger.warning(
            "⚠️ Redis 连接失败：full_evaluation_v2 幂等锁将不可用，"
            "极端情况下可能重复创建评估工作流", exc_info=True,
        )

    yield  # 应用运行中

    # ---- 关闭阶段 ----
    logger.info("🛑 应用关闭，清理资源")


# ── FastAPI 实例 ──
app = FastAPI(
    title="特种设备缺陷解析与FMEA评估系统",
    description="多智能体协同复杂工作流工业级评估",
    version="4.5.0",
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
    return openapi_schema


# 替换默认的 openapi() 方法
app.openapi = custom_openapi
# ═══════════════════════════════════════════════════════

# ── 挂载安全中间件（必须在路由注册之前） ──
app.add_middleware(APIKeyMiddleware)

# ── 注册路由 ──
app.include_router(project_router)
app.include_router(analysis_router)
app.include_router(fmea_router)


# ═══════════════════════════════════════════════════════
#  v4.5.0 全局异常处理器（结构化错误输出）
# ═══════════════════════════════════════════════════════
@app.exception_handler(FMEABaseException)
async def fmea_exception_handler(request: Request, exc: FMEABaseException):
    """业务异常统一映射为结构化 HTTP 错误（与路由层行为一致）"""
    logger.warning("全局业务异常 [%s]: %s", exc.error_code, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "detail": exc.detail,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常处理器：记录完整堆栈，返回结构化 500"""
    logger.exception("未处理异常: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_ERROR",
            "message": f"系统内部错误: {exc}",
        },
    )


# ── 根端点 ──
@app.get("/", tags=["系统信息"])
async def root():
    return {
        "service": "特种设备缺陷解析与FMEA评估",
        "version": "4.5.0",
        "docs": "/docs",
        "redoc": "/redoc",
    }


# ── 健康检查（v4.5.0 新增）──
@app.get("/health", tags=["系统信息"])
async def health_check():
    """
    健康检查端点：返回数据库与 Redis 连通状态。

    说明：
    - 恒返回 HTTP 200，具体健康度由 JSON 的 status 字段表达，
      便于负载均衡/监控系统解析。
    - status=ok：全部依赖正常；status=degraded：至少一项依赖异常。
    - Redis 不可用时 full_evaluation_v2 幂等锁失效，可能重复创建工作流。
    """
    db_ok = False
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.warning("健康检查：数据库不可用: %s", e)

    redis_ok = False
    try:
        from app.tasks.analysis import redis_client

        redis_ok = bool(redis_client.ping())
    except Exception as e:
        logger.warning("健康检查：Redis 不可用: %s", e)

    overall = "ok" if (db_ok and redis_ok) else "degraded"
    return {
        "status": overall,
        "db": db_ok,
        "redis": redis_ok,
        "version": app.version,
    }


# ── 直接运行入口 ──

# ?? uvicorn 0.24 + litellm SecretRedactionFilter ?????? ??
# litellm 1.97 ? uvicorn.error ??? SecretRedactionFilter?? getMessage() ???? args=None?
# uvicorn 0.24 ??????TTY???? color_message????? %d / %s://%s:%d ????????
# getMessage()??? args ?? ? banner ???? %d/%s?uvicorn.error ??? uvicorn ???????
# ?????????????????litellm ?? handler ?????????????
import logging as _logging
try:
    import litellm  # noqa: F401  # ?? litellm._logging ??????????????????
except ImportError:
    pass
for _name in ("uvicorn.error", "uvicorn.access", "uvicorn"):
    _lg = _logging.getLogger(_name)
    _lg.filters = [f for f in _lg.filters if type(f).__name__ != "SecretRedactionFilter"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )