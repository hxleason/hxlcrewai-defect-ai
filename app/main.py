from fastapi import FastAPI
from app.api.routes import router as api_router
from app.db.base import Base
from app.db.session import engine

# 创建所有表
Base.metadata.create_all(bind=engine)

app = FastAPI(title="FMEA AI Agent")

app.include_router(api_router)
