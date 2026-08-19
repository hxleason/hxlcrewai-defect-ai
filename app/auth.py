from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import os

# 从环境变量读取合法 API Key 列表
API_KEYS = set(
    key.strip() for key in os.getenv("API_KEYS", "").split(",") if key.strip()
)

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 放行文档页面和根路径
        if request.url.path in ["/docs", "/redoc", "/openapi.json", "/"]:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if not api_key or api_key not in API_KEYS:
            # 关键修复：返回 JSONResponse 而不是 raise HTTPException
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API Key"},
            )

        # 注入用户身份，供后续端点使用
        request.state.user = "api_user"  # 可扩展为从 token 解析
        return await call_next(request)