"""
分析任务路由
处理缺陷提取、FMEA评估等任务的提交与状态查询
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any
import logging

from app.db.database import get_db          # ✅ 统一数据库入口
from app.models import Task                 # ✅ 直接从 app.models 导入
from app.tasks.analysis import (
    analysis_task,
    evaluation_task,
    full_evaluation_task,
)

logger = logging.getLogger(__name__)
router = APIRouter()                        # 不再定义根路由，避免与主应用冲突


# ──────────── 请求/响应模型 ────────────
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

    model_config = ConfigDict(from_attributes=True)


# ──────────── 内部辅助函数 ────────────
def _create_task_with_celery(
    db: Session,
    task_type: str,
    input_text: str,
    celery_func,
) -> TaskStatusResponse:
    """
    在同一个事务内创建 Task 记录并投递 Celery 任务。
    任何步骤失败均整体回滚，保证数据一致性。
    """
    try:
        # 1) 新建任务记录（暂不 commit，仅 flush 获取 id）
        task = Task(
            type=task_type,
            input_text=input_text,
            status="pending",
            progress=0,
        )
        db.add(task)
        db.flush()                           # 获得 task.id，但仍处于事务中

        # 2) 投递 Celery 异步任务
        celery_result = celery_func.delay(
            task_id=task.id,
            input_text=input_text,
        )

        # 3) 记录 celery_task_id
        task.celery_task_id = celery_result.id

        # 4) 整体提交事务
        db.commit()
        db.refresh(task)                     # 获取最新字段（可选）

        return TaskStatusResponse(
            task_id=task.id,
            type=task_type,
            status="pending",
            progress=0,
            celery_task_id=celery_result.id,
        )
    except Exception as exc:
        db.rollback()
        logger.error("创建 %s 任务失败: %s", task_type, exc)
        raise HTTPException(status_code=500, detail="无法创建任务")


# ──────────── 业务端点 ────────────
@router.post("/analyze", response_model=TaskStatusResponse, status_code=202)
def submit_analysis(
    request: AnalysisRequest,
    db: Session = Depends(get_db),
):
    """提交缺陷提取任务（解析 Agent）"""
    return _create_task_with_celery(db, "analysis", request.input_text, analysis_task)


@router.post("/evaluate", response_model=TaskStatusResponse, status_code=202)
def submit_evaluation(
    request: AnalysisRequest,
    db: Session = Depends(get_db),
):
    """提交 FMEA 评估任务（解析 + 诊断 Agent）"""
    return _create_task_with_celery(db, "evaluation", request.input_text, evaluation_task)


@router.post("/full-evaluate", response_model=TaskStatusResponse, status_code=202)
def submit_full_evaluation(
    request: AnalysisRequest,
    db: Session = Depends(get_db),
):
    """提交完整评估任务（解析 + 诊断 + 法规审核 Agent）"""
    return _create_task_with_celery(db, "full", request.input_text, full_evaluation_task)


@router.get("/task/{task_id}", response_model=TaskStatusResponse)
def get_task_status(
    task_id: int,
    db: Session = Depends(get_db),
):
    """查询任务状态与结果"""
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