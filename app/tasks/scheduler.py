"""
周期性调度任务：检测长时间未响应的 started 任务，标记为失败
由 Celery Beat 触发，建议每 5 分钟执行一次
"""
import datetime
import logging

from sqlalchemy import or_

try:
    from app.celery_app import celery_app
except ModuleNotFoundError:
    from app.tasks.celery_app import celery_app

from app.db.database import SessionLocal
from app.models import Task

logger = logging.getLogger(__name__)


@celery_app.task()   # 不指定 name，自动使用模块路径 "app.tasks.scheduler.check_stuck_tasks"
def check_stuck_tasks():
    """
    查询状态为 'started' 且满足以下任一条件的任务：
      · last_heartbeat 早于 15 分钟前
      · last_heartbeat 为 NULL，但 started_at 早于 15 分钟前（兼容旧数据）
    将这些任务标记为 failure，并写入错误码 TASK_STUCK。
    """
    now = datetime.datetime.utcnow()
    threshold = now - datetime.timedelta(minutes=15)

    with SessionLocal() as db:
        stuck_tasks = db.query(Task).filter(
            Task.status == "started",
            or_(
                Task.last_heartbeat < threshold,
                Task.last_heartbeat.is_(None) & (Task.started_at < threshold)
            )
        ).all()

        if not stuck_tasks:
            logger.debug("No stuck tasks found.")
            return {"cleaned": 0}

        count = 0
        for task in stuck_tasks:
            task.status = "failure"
            task.error_message = "任务可能因 Worker 崩溃而中断"
            task.result = {
                "error_code": "TASK_STUCK",
                "detail": "任务长时间无响应，已被系统自动标记为失败",
            }
            task.completed_at = now   # 记录失败时间
            count += 1

        db.commit()
        logger.info("Cleaned %d stuck tasks.", count)
        return {"cleaned": count}