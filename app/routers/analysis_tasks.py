"""
分析任务路由
—— 处理缺陷提取、FMEA评估、完整评估、人工审核等任务的提交与状态查询
   失败时返回统一的 error_code 和 message，便于前端差异化提示
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any, List
import logging

from app.db.database import get_db
from app.models import Task
from app.tasks.analysis import (
    analysis_task,
    evaluation_task,
    full_evaluation_task,      # 保留旧版完整评估（向前兼容）
    full_evaluation_v2,        # 新版高效全流程
    continue_after_review,     # 人工审核后继续
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ══════════════════ 请求/响应模型 ══════════════════
class AnalysisRequest(BaseModel):
    input_text: str = Field(..., min_length=10, description="待分析的报告文本")


class ErrorDetail(BaseModel):
    """结构化错误信息，失败时返回给前端"""
    error_code: Optional[str] = None
    message: Optional[str] = None


class TaskStatusResponse(BaseModel):
    task_id: int
    type: str
    status: str
    progress: int
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    error: Optional[ErrorDetail] = None
    celery_task_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ReviewDecision(BaseModel):
    """单条缺陷的人工审核决策"""
    defect_id: int = Field(..., description="缺陷的唯一 ID")
    action: str = Field(..., description="操作：accept / reject / modify")
    comment: Optional[str] = Field(None, description="审核备注")


# ══════════════════ 内部辅助 ══════════════════
def _create_task_with_celery(
    db: Session,
    task_type: str,
    input_text: str,
    celery_func,
) -> TaskStatusResponse:
    """
    在同一个事务内创建 Task 记录并投递 Celery 任务。
    失败时整体回滚，并返回包含 error_code 的结构化错误。
    """
    try:
        task = Task(
            type=task_type,
            input_text=input_text,
            status="pending",
            progress=0,
        )
        db.add(task)
        db.flush()

        celery_result = celery_func.delay(
            task_id=task.id,
            input_text=input_text,
        )

        task.celery_task_id = celery_result.id
        db.commit()
        db.refresh(task)

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
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "TASK_CREATION_FAILED",
                "message": f"创建任务失败: {str(exc)}",
            },
        )


# ══════════════════ 业务端点 ══════════════════
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
    """
    提交完整评估任务（新版高效流水线）：
    提取 → 并行评级 → 高风险检查(可挂起) → 并行法规 → 汇总
    """
    return _create_task_with_celery(db, "full", request.input_text, full_evaluation_v2)


@router.post("/task/{task_id}/review", status_code=200)
def submit_review(
    task_id: int,
    decisions: List[ReviewDecision],
    db: Session = Depends(get_db),
):
    """
    人工审核高风险缺陷后提交决策。
    任务必须处于 pending_review 状态，否则返回 400。
    """
    # 验证任务状态
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status != "pending_review":
        raise HTTPException(status_code=400, detail="任务未处于待审核状态")

    # 将决策转换为字典（key 为 defect_id 字符串）
    decision_dict = {str(d.defect_id): d.dict() for d in decisions}

    # 触发异步继续执行
    continue_after_review.delay(task_id, decision_dict)

    return {"status": "review_submitted", "task_id": task_id}


@router.get("/task/{task_id}", response_model=TaskStatusResponse)
def get_task_status(
    task_id: int,
    db: Session = Depends(get_db),
):
    """
    查询任务状态与结果。
    若任务失败，会从数据库的 result 字段中提取 error_code 并放入 error 对象，
    前端可统一处理 error.error_code 而无需自行解析。
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # ── 构造结构化的错误对象 ──
    error_detail = None
    if task.status == "failure":
        if isinstance(task.result, dict):
            error_code = task.result.get("error_code")
            message = task.result.get("detail") or task.result.get("message") or task.error_message
            if error_code or message:
                error_detail = ErrorDetail(error_code=error_code, message=message)

        if not error_detail:
            error_detail = ErrorDetail(
                error_code="UNKNOWN_ERROR",
                message=task.error_message or "任务执行失败",
            )

    return TaskStatusResponse(
        task_id=task.id,
        type=task.type,
        status=task.status,
        progress=task.progress,
        result=task.result,
        error_message=task.error_message,
        error=error_detail,
        celery_task_id=task.celery_task_id,
    )