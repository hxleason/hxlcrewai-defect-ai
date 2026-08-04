"""
Celery 任务定义 —— 多智能体 FMEA 分析系统
所有任务均需传入已有的 Task.id，该任务在 API 层已创建且 type 字段正确：
  - analysis_task      → Task.type = "analysis"
  - evaluation_task    → Task.type = "evaluation"
  - full_evaluation_task → Task.type = "full"
"""

import os
import datetime
import traceback
import logging
from typing import Any, Dict

# ------- 自动适配 celery_app 位置 -------
try:
    from app.celery_app import celery_app
except ModuleNotFoundError:
    from app.tasks.celery_app import celery_app

# ‼️ 如果 SessionLocal 实际位置不同，请修改下面这行 ‼️
from app.db.database import SessionLocal      # 可能是 from app.db.base 或 app.db.session
from app.models import Task
from app.crew import (
    create_analysis_crew,
    run_fmea_evaluation,
    run_full_fmea_evaluation,
)

logger = logging.getLogger(__name__)


# ================== 数据库辅助函数 ==================
def update_task(task_id: int, **kwargs) -> None:
    """
    安全更新 Task 记录。
    可更新字段：status, progress, result, error_message, celery_task_id, completed_at 等。
    注意：type 字段在任务创建时固定，此处不修改。
    """
    with SessionLocal() as db:
        try:
            task = db.query(Task).filter(Task.id == task_id).first()
            if not task:
                raise ValueError(f"Task {task_id} 不存在")
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"更新任务 {task_id} 失败: {e}")
            raise
    # with 语句自动关闭 session


def check_task_type(task_id: int, expected_type: str) -> None:
    """校验任务类型是否匹配，防止用错 Celery 任务"""
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} 不存在")
        if task.type != expected_type:
            raise ValueError(
                f"Task {task_id} 类型为 '{task.type}'，但 Celery 任务期望 '{expected_type}'"
            )


# ================== Celery 任务 ==================
@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def analysis_task(self, task_id: int, input_text: str) -> Dict[str, Any]:
    """仅缺陷提取（解析 Agent） —— 要求 Task.type == 'analysis'"""
    try:
        check_task_type(task_id, "analysis")
        update_task(
            task_id,
            status="started",
            progress=10,
            celery_task_id=self.request.id,
        )
        raw_result = create_analysis_crew(input_text)
        update_task(
            task_id,
            status="success",
            progress=100,
            result={"raw": raw_result},
            completed_at=datetime.datetime.utcnow(),
        )
        return {"task_id": task_id, "status": "success"}

    except Exception as e:
        logger.error(f"analysis_task {task_id} 失败: {traceback.format_exc()}")
        # 若已达最大重试次数，直接标记最终失败，不再重试
        if self.request.retries >= self.max_retries:
            update_task(task_id, status="failure", error_message=str(e))
            raise  # 让 Celery 标记任务为 FAILURE
        update_task(task_id, status="failure", error_message=str(e))
        raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def evaluation_task(self, task_id: int, input_text: str) -> Dict[str, Any]:
    """FMEA 评估（解析 + 诊断 Agent） —— 要求 Task.type == 'evaluation'"""
    try:
        check_task_type(task_id, "evaluation")
        update_task(
            task_id,
            status="started",
            progress=10,
            celery_task_id=self.request.id,
        )
        result_dict = run_fmea_evaluation(input_text)
        update_task(
            task_id,
            status="success",
            progress=100,
            result=result_dict,
            completed_at=datetime.datetime.utcnow(),
        )
        return {"task_id": task_id, "status": "success"}

    except Exception as e:
        logger.error(f"evaluation_task {task_id} 失败: {traceback.format_exc()}")
        if self.request.retries >= self.max_retries:
            update_task(task_id, status="failure", error_message=str(e))
            raise
        update_task(task_id, status="failure", error_message=str(e))
        raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=120)
def full_evaluation_task(self, task_id: int, input_text: str) -> Dict[str, Any]:
    """完整评估（解析 + 诊断 + 法规审核 Agent） —— 要求 Task.type == 'full'

    向量库预加载已由 app.rag.vector_store 模块完成，
    run_full_fmea_evaluation 内部可直接使用预加载的向量库进行法规检索。
    """
    try:
        check_task_type(task_id, "full")
        update_task(
            task_id,
            status="started",
            progress=10,
            celery_task_id=self.request.id,
        )
        result_dict = run_full_fmea_evaluation(input_text)
        update_task(
            task_id,
            status="success",
            progress=100,
            result=result_dict,
            completed_at=datetime.datetime.utcnow(),
        )
        return {"task_id": task_id, "status": "success"}

    except Exception as e:
        logger.error(f"full_evaluation_task {task_id} 失败: {traceback.format_exc()}")
        if self.request.retries >= self.max_retries:
            update_task(task_id, status="failure", error_message=str(e))
            raise
        update_task(task_id, status="failure", error_message=str(e))
        raise self.retry(exc=e)