"""
分析任务路由（安全增强版）
-----------------------------------
提供缺陷提取、FMEA评估、完整评估、人工审核任务的提交与状态查询。
审核端点已加入并发锁与输入校验，确保数据一致性与操作幂等。

风险等级统一说明：
    全系统已统一采用四级风险等级：低(1)/中(2)/高(3)/极高(4)。
    本文件不直接处理风险等级文字，相关逻辑位于 app/tasks/analysis.py 及 app/core/utils.py。
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings  # 便于后续使用阈值等配置
from app.db.database import get_db
from app.models import Task
from app.tasks.analysis import (
    analysis_task,
    continue_after_review,
    evaluation_task,
    full_evaluation_task,  # 旧版，保留向前兼容
    full_evaluation_v2,    # 新版高效全流程
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================
# 请求 / 响应模型（统一命名，增强校验）
# ============================================================
class AnalysisRequest(BaseModel):
    """分析/评估请求"""
    input_text: str = Field(
        ...,
        min_length=10,
        max_length=100_000,
        description="待分析的报告文本（10~100000 字符）",
    )


class ErrorDetail(BaseModel):
    """结构化错误信息，前端可统一处理 error_code 做差异化提示"""
    error_code: Optional[str] = None
    message: Optional[str] = None


class TaskStatusResponse(BaseModel):
    """任务状态统一响应"""
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
    """单条缺陷的人工审核决策（增强校验）"""
    defect_id: int = Field(..., ge=1, description="缺陷唯一 ID（正整数）")
    action: str = Field(
        ...,
        description="操作：accept（接受） / reject（拒绝） / modify（修改）",
    )
    comment: Optional[str] = Field(
        None,
        max_length=2000,
        description="审核备注（可选，最长 2000 字符）",
    )

    @field_validator("action")
    @classmethod
    def action_must_be_valid(cls, v: str) -> str:
        allowed = {"accept", "reject", "modify"}
        if v not in allowed:
            raise ValueError(f"action 必须是 {allowed} 之一")
        return v

    @field_validator("comment")
    @classmethod
    def comment_no_control_chars(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            # 简单过滤控制字符，避免注入风险
            v = v.replace("\x00", "")
        return v


class ReviewResponse(BaseModel):
    """审核提交后的响应"""
    status: str
    task_id: int
    new_task_status: str
    decisions_count: int


# ============================================================
# 内部辅助函数
# ============================================================
def _create_task_with_celery(
    db: Session,
    task_type: str,
    input_text: str,
    celery_func,
) -> TaskStatusResponse:
    """
    在同一个事务内创建 Task 记录并投递 Celery 任务。
    若任何环节失败则整体回滚，前端统一收到错误详情。
    """
    try:
        task = Task(
            type=task_type,
            input_text=input_text,
            status="pending",
            progress=0,
        )
        db.add(task)
        db.flush()  # 获取 task.id

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
        logger.error("创建任务 %s 失败: %s", task_type, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "TASK_CREATION_FAILED",
                "message": f"任务创建失败：{str(exc)}",
            },
        )


# ============================================================
# 业务端点
# ============================================================
@router.post(
    "/analyze",
    response_model=TaskStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交缺陷提取任务",
    description="启动解析 Agent，从文本中提取特种设备缺陷结构。",
)
def submit_analysis(
    request: AnalysisRequest,
    db: Session = Depends(get_db),
):
    return _create_task_with_celery(db, "analysis", request.input_text, analysis_task)


@router.post(
    "/evaluate",
    response_model=TaskStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交 FMEA 评估任务",
    description="依次运行解析与诊断 Agent，生成 FMEA 评估结果。",
)
def submit_evaluation(
    request: AnalysisRequest,
    db: Session = Depends(get_db),
):
    return _create_task_with_celery(db, "evaluation", request.input_text, evaluation_task)


@router.post(
    "/full-evaluate",
    response_model=TaskStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交完整评估任务（新版流水线）",
    description="""
    高效全流程评估：
    1. 缺陷提取 → 2. 并行风险评估 → 3. 高风险缺陷审查(可挂起) → 4. 并行法规匹配 → 5. 结果汇总。
    """,
)
def submit_full_evaluation(
    request: AnalysisRequest,
    db: Session = Depends(get_db),
):
    return _create_task_with_celery(db, "full", request.input_text, full_evaluation_v2)


@router.post(
    "/task/{task_id}/review",
    response_model=ReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="提交人工审核决策",
    description="""
    针对高风险缺陷的人工审核决策提交。
    - 任务必须处于 `pending_review` 状态。
    - 使用数据库悲观锁防止并发重复提交。
    - 提交成功后任务状态将立即更新为 `processing`，避免中途被二次修改。
    """,
)
def submit_review(
    task_id: int,
    decisions: List[ReviewDecision],
    db: Session = Depends(get_db),
):
    # ── 1. 悲观锁获取任务行，确保并发安全 ──
    task = (
        db.query(Task)
        .filter(Task.id == task_id)
        .with_for_update()
        .first()
    )
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "TASK_NOT_FOUND", "message": "任务不存在"},
        )

    if task.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "INVALID_TASK_STATE",
                "message": f"任务当前状态为 '{task.status}'，必须为 'pending_review' 才能提交审核。",
            },
        )

    # ── 2. 转换决策为可序列化字典（key 为字符串 defect_id） ──
    try:
        decision_dict = {str(d.defect_id): d.model_dump() for d in decisions}
    except Exception as e:
        logger.error("决策数据序列化失败: %s", e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "DECISION_SERIALIZATION_ERROR", "message": "决策数据格式错误"},
        )

    # ── 3. 更新任务状态为中间态，防止竞态 ──
    original_status = task.status
    task.status = "processing"  # 表示正在处理审核
    try:
        db.flush()  # 确保状态变更立即生效

        # ── 4. 投递异步任务 ──
        celery_task = continue_after_review.delay(task_id, decision_dict)
        logger.info(
            "审核任务 %d 已投递，Celery ID: %s，决策数量: %d",
            task_id,
            celery_task.id,
            len(decisions),
        )

        # ── 5. 事务提交（状态更新 + Celery 投递承诺） ──
        db.commit()
    except Exception as exc:
        # 回滚状态变更
        db.rollback()
        logger.error("审核任务 %d 提交失败: %s", task_id, exc, exc_info=True)
        # 尝试将任务状态恢复（新事务中执行，避免遗留在错误状态）
        try:
            task.status = original_status
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "REVIEW_SUBMISSION_FAILED",
                "message": f"审核任务提交失败：{str(exc)}",
            },
        )

    return ReviewResponse(
        status="review_submitted",
        task_id=task_id,
        new_task_status=task.status,
        decisions_count=len(decisions),
    )


@router.get(
    "/task/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询任务状态与结果",
)
def get_task_status(
    task_id: int,
    db: Session = Depends(get_db),
):
    """获取任务最新状态，失败时自动解析 error_code。"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "TASK_NOT_FOUND", "message": "任务不存在"},
        )

    # 构造结构化的错误对象，便于前端处理
    error_detail = None
    if task.status == "failure":
        if isinstance(task.result, dict):
            error_code = task.result.get("error_code")
            message = (
                task.result.get("detail")
                or task.result.get("message")
                or task.error_message
            )
            if error_code or message:
                error_detail = ErrorDetail(error_code=error_code, message=message)

        if not error_detail:  # 兜底
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