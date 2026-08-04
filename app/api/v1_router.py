"""
API v1 路由定义（适配 app/models.py 的 Task 模型）
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Dict, Any
import logging

from app.db.database import SessionLocal   # 你的数据库会话工厂，根据实际路径调整
from app.models import Task                # ✅ 直接从 app.models 导入
from app.tasks.analysis import (
    analysis_task,
    evaluation_task,
    full_evaluation_task,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------- Pydantic 模型 ----------
class AnalysisRequest(BaseModel):
    input_text: str = Field(..., min_length=10, description="待分析的报告文本")


class TaskStatusResponse(BaseModel):
    task_id: int
    type: str
    status: str
    progress: int
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    celery_task_id: Optional[str] = None

    # Pydantic v2 写法（若用的是 v1 请换成 class Config: orm_mode = True）
    model_config = ConfigDict(from_attributes=True)


# ---------- 根路由 ----------
@router.get("/")
async def root():
    return {
        "message": "特种设备缺陷解析与FMEA评估服务 v4.3 已启动",
        "docs": "/docs",
        "redoc": "/redoc"
    }


# ---------- 辅助：创建 Task 并启动 Celery ----------
def _create_and_launch(task_type: str, input_text: str, celery_func):
    """通用函数：创建 Task 记录，启动 Celery 任务，返回初始状态"""
    db = SessionLocal()
    try:
        # 1. 存入数据库（type 和 input_text 必填，celery_task_id 稍后更新）
        task = Task(
            type=task_type,
            input_text=input_text,
            status="pending",
            progress=0,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id

        # 2. 异步执行 Celery 任务
        celery_result = celery_func.delay(task_id=task_id, input_text=input_text)

        # 3. 将 Celery 任务 ID 写回记录
        task.celery_task_id = celery_result.id
        db.commit()

        # 返回给前端的信息
        return TaskStatusResponse(
            task_id=task_id,
            type=task_type,
            status="pending",
            progress=0,
            celery_task_id=celery_result.id,
        )
    except Exception as e:
        db.rollback()
        logger.error(f"创建 {task_type} 任务失败: {e}")
        raise HTTPException(status_code=500, detail="无法创建任务")
    finally:
        db.close()


# ---------- 业务端点 ----------
@router.post("/analyze", response_model=TaskStatusResponse, status_code=202)
def submit_analysis(request: AnalysisRequest):
    """提交缺陷提取任务（解析 Agent）"""
    return _create_and_launch("analysis", request.input_text, analysis_task)


@router.post("/evaluate", response_model=TaskStatusResponse, status_code=202)
def submit_evaluation(request: AnalysisRequest):
    """提交 FMEA 评估任务（解析 + 诊断 Agent）"""
    return _create_and_launch("evaluation", request.input_text, evaluation_task)


@router.post("/full-evaluate", response_model=TaskStatusResponse, status_code=202)
def submit_full_evaluation(request: AnalysisRequest):
    """提交完整评估任务（解析 + 诊断 + 法规审核 Agent）"""
    return _create_and_launch("full", request.input_text, full_evaluation_task)


@router.get("/task/{task_id}", response_model=TaskStatusResponse)
def get_task_status(task_id: int):
    """查询任务状态与结果"""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        return TaskStatusResponse(
            task_id=task.id,
            type=task.type,
            status=task.status,
            progress=task.progress,
            result=task.result,
            error_message=task.error_message,
            celery_task_id=task.celery_task_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询任务 {task_id} 失败: {e}")
        raise HTTPException(status_code=500, detail="查询任务失败")
    finally:
        db.close()