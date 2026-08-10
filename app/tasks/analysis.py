"""
app/tasks/analysis.py
Celery 任务定义 —— 多智能体 FMEA 分析系统（终极版 v4.1 + 字段防丢失）
功能：
  - 保留原有 v3.1 所有任务，向前兼容
  - 新增 full_evaluation_v2：提取 → 并行评级 → 高风险检查(可挂起) → 并行法规 → 汇总
  - **新增** 自动修复缺失 defect type 字段，彻底杜绝字段传递丢失
  - 支持提前终止（无缺陷直接成功）
  - 支持人机协同（RPN>150 挂起，审核后继续）
  - **新增** Redis 幂等锁，防止任务重试导致 chord 重复创建
"""

import datetime
import traceback
import logging
from typing import Any, Dict, List

import redis
from celery import group, chain, chord
from celery.exceptions import SoftTimeLimitExceeded

# 自动适配 celery_app 位置（兼容不同目录结构）
try:
    from app.celery_app import celery_app
except ModuleNotFoundError:
    from app.tasks.celery_app import celery_app

from app.db.database import SessionLocal
from app.models import Task
from app.core.config import settings
from app.crew import (
    create_analysis_crew,
    run_fmea_evaluation,
    run_full_fmea_evaluation,
)
from app.core.exceptions import FMEABaseException
from app.core.defect_processor import (
    extract_defects,
    evaluate_one_defect,
    audit_one_defect,
)

# ── 基于现有配置创建 Redis 客户端（取代 app.extensions） ──
redis_client = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True
)

logger = logging.getLogger(__name__)

LOCK_PREFIX = "fmea:task:lock:"
LOCK_EXPIRE = 3600


# ================== 错误码常量 ==================
class ErrorCode:
    TASK_TIMEOUT = "TASK_TIMEOUT"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    TASK_STUCK = "TASK_STUCK"


# ================== 数据库辅助函数 ==================
def update_task(task_id: int, **kwargs) -> None:
    """安全更新 Task 记录，自动管理会话。"""
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
            logger.error("更新任务 %s 失败: %s", task_id, e)
            raise


def check_task_type(task_id: int, expected_type: str) -> None:
    """校验任务类型是否匹配。"""
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} 不存在")
        if task.type != expected_type:
            raise ValueError(
                f"Task {task_id} 类型为 '{task.type}'，但 Celery 任务期望 '{expected_type}'"
            )


def heartbeat(task_id: int):
    """更新任务最近一次心跳时间（用于关键节点）。"""
    update_task(task_id, last_heartbeat=datetime.datetime.utcnow())


# ================== 幂等性工具函数 ==================
def _acquire_task_lock(task_id: int) -> bool:
    key = f"{LOCK_PREFIX}{task_id}"
    if redis_client.setnx(key, "1") == 0:
        return False
    redis_client.expire(key, LOCK_EXPIRE)
    return True


def _release_task_lock(task_id: int):
    key = f"{LOCK_PREFIX}{task_id}"
    redis_client.delete(key)


# ================== 字段修复函数 ==================
# 常见特种设备缺陷关键词（可根据实际业务扩展）
KNOWN_DEFECT_TYPES = [
    "裂纹", "咬边", "气孔", "未熔合", "未焊透",
    "夹渣", "变形", "腐蚀", "磨损", "凹坑",
    "鼓包", "错边", "焊瘤", "飞溅", "渗漏"
]


def _normalize_defect_type(defect: dict) -> dict:
    """
    确保缺陷字典中存在非空的 type 字段。
    若缺失，则尝试从 original_text 或 location 中提取已知关键词。
    如果仍然无法识别，则置为“未知缺陷”并记录告警。
    """
    if defect.get("type"):
        return defect

    # 收集所有可能包含缺陷信息的文本
    text = f"{defect.get('original_text','')} {defect.get('location','')}".lower()
    for keyword in KNOWN_DEFECT_TYPES:
        if keyword in text:
            defect["type"] = keyword
            logger.warning("补充缺陷 %s 的 type = '%s'（从文本推断）", defect.get("id"), keyword)
            return defect

    # 兜底
    defect["type"] = "未知缺陷"
    logger.warning("缺陷 %s 的 type 缺失且无法自动推断，已设为 '未知缺陷'", defect.get("id"))
    return defect


# ================== 旧版任务（保留不动） ==================
@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    time_limit=600,
    soft_time_limit=540,
)
def analysis_task(self, task_id: int, input_text: str) -> Dict[str, Any]:
    """仅缺陷提取（解析 Agent）—— 原版保留"""
    try:
        check_task_type(task_id, "analysis")
        now = datetime.datetime.utcnow()
        update_task(
            task_id,
            status="started",
            progress=10,
            celery_task_id=self.request.id,
            started_at=now,
            last_heartbeat=now,
        )
        raw_result = create_analysis_crew(input_text)
        heartbeat(task_id)
        update_task(
            task_id,
            status="success",
            progress=100,
            result={"raw": raw_result},
            completed_at=datetime.datetime.utcnow(),
        )
        return {"task_id": task_id, "status": "success"}

    except SoftTimeLimitExceeded:
        logger.error("analysis_task %s 执行超时", task_id)
        update_task(task_id, status="failure", error_message="任务执行超时",
                    result={"error_code": ErrorCode.TASK_TIMEOUT, "detail": "任务执行时间超过限制"})
        return {"task_id": task_id, "status": "timeout"}

    except FMEABaseException as e:
        logger.error("analysis_task %s 业务异常: %s", task_id, e.message)
        update_task(task_id, status="failure", error_message=e.message,
                    result={"error_code": e.error_code, "detail": e.message})
        return {"task_id": task_id, "status": "business_error"}

    except Exception as e:
        logger.error("analysis_task %s 系统异常: %s", task_id, traceback.format_exc())
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        update_task(task_id, status="failure", error_message=str(e),
                    result={"error_code": ErrorCode.SYSTEM_ERROR, "detail": str(e)})
        return {"task_id": task_id, "status": "system_error"}


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    time_limit=1200,
    soft_time_limit=1080,
)
def evaluation_task(self, task_id: int, input_text: str) -> Dict[str, Any]:
    """FMEA 评估（解析 + 诊断 Agent）—— 原版保留"""
    try:
        check_task_type(task_id, "evaluation")
        now = datetime.datetime.utcnow()
        update_task(
            task_id,
            status="started",
            progress=10,
            celery_task_id=self.request.id,
            started_at=now,
            last_heartbeat=now,
        )
        result_dict = run_fmea_evaluation(input_text)
        heartbeat(task_id)
        update_task(
            task_id,
            status="success",
            progress=100,
            result=result_dict,
            completed_at=datetime.datetime.utcnow(),
        )
        return {"task_id": task_id, "status": "success"}

    except SoftTimeLimitExceeded:
        logger.error("evaluation_task %s 执行超时", task_id)
        update_task(task_id, status="failure", error_message="任务执行超时",
                    result={"error_code": ErrorCode.TASK_TIMEOUT, "detail": "任务执行时间超过限制"})
        return {"task_id": task_id, "status": "timeout"}

    except FMEABaseException as e:
        logger.error("evaluation_task %s 业务异常: %s", task_id, e.message)
        update_task(task_id, status="failure", error_message=e.message,
                    result={"error_code": e.error_code, "detail": e.message})
        return {"task_id": task_id, "status": "business_error"}

    except Exception as e:
        logger.error("evaluation_task %s 系统异常: %s", task_id, traceback.format_exc())
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        update_task(task_id, status="failure", error_message=str(e),
                    result={"error_code": ErrorCode.SYSTEM_ERROR, "detail": str(e)})
        return {"task_id": task_id, "status": "system_error"}


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    time_limit=1800,
    soft_time_limit=1620,
)
def full_evaluation_task(self, task_id: int, input_text: str) -> Dict[str, Any]:
    """完整评估（解析 + 诊断 + 法规审核 Agent）—— 原版保留，不再推荐使用"""
    try:
        check_task_type(task_id, "full")
        now = datetime.datetime.utcnow()
        update_task(
            task_id,
            status="started",
            progress=10,
            celery_task_id=self.request.id,
            started_at=now,
            last_heartbeat=now,
        )
        result_dict = run_full_fmea_evaluation(input_text)
        heartbeat(task_id)
        update_task(
            task_id,
            status="success",
            progress=100,
            result=result_dict,
            completed_at=datetime.datetime.utcnow(),
        )
        return {"task_id": task_id, "status": "success"}

    except SoftTimeLimitExceeded:
        logger.error("full_evaluation_task %s 执行超时", task_id)
        update_task(task_id, status="failure", error_message="任务执行超时",
                    result={"error_code": ErrorCode.TASK_TIMEOUT, "detail": "任务执行时间超过限制"})
        return {"task_id": task_id, "status": "timeout"}

    except FMEABaseException as e:
        logger.error("full_evaluation_task %s 业务异常: %s", task_id, e.message)
        update_task(task_id, status="failure", error_message=e.message,
                    result={"error_code": e.error_code, "detail": e.message})
        return {"task_id": task_id, "status": "business_error"}

    except Exception as e:
        logger.error("full_evaluation_task %s 系统异常: %s", task_id, traceback.format_exc())
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        update_task(task_id, status="failure", error_message=str(e),
                    result={"error_code": ErrorCode.SYSTEM_ERROR, "detail": str(e)})
        return {"task_id": task_id, "status": "system_error"}


# ================== 新版高效全流程任务（v2，幂等安全 + 字段防丢） ==================

@celery_app.task(bind=True, max_retries=2)
def full_evaluation_v2(self, task_id: int, input_text: str):
    """
    终极版完整评估（幂等优化 + 字段自动修复）：
    提取 → 并行评级 → 高风险检查(可挂起) → 并行法规 → 汇总。
    通过 Redis 锁确保即使 Celery 重试也不会重复创建 chord。
    支持提前终止、人机协同。
    """
    if not _acquire_task_lock(task_id):
        logger.warning("任务 %s 已启动工作流，跳过重复执行", task_id)
        return {"status": "skipped", "reason": "duplicate"}

    logger.info("首次启动评估工作流，task_id=%s", task_id)

    try:
        check_task_type(task_id, "full")
        now = datetime.datetime.utcnow()
        update_task(
            task_id,
            status="started",
            progress=5,
            celery_task_id=self.request.id,
            started_at=now,
            last_heartbeat=now,
        )

        # 1. 提取缺陷（仅一次 LLM 调用）
        defects = extract_defects(input_text)

        # ── 字段防丢：从源头保证每个缺陷都有 type ──
        defects = [_normalize_defect_type(d) for d in defects]
        logger.debug("提取到的缺陷（已规范化）：%s", defects)

        heartbeat(task_id)

        # 2. 提前终止：无缺陷
        if not defects:
            update_task(
                task_id,
                status="success",
                progress=100,
                result={"defects": [], "report_summary": "未发现需评估的缺陷"},
                completed_at=datetime.datetime.utcnow(),
            )
            _release_task_lock(task_id)
            return {"task_id": task_id, "status": "no_defects"}

        update_task(task_id, progress=20)

        # 3. 并行评估所有缺陷（纯 Python，无 LLM）
        job = group(
            evaluate_single_defect.s(task_id, defect)
            for defect in defects
        )
        chord(job, review_and_proceed.s(task_id)).apply_async()
        logger.info("Chord 已创建，task_id=%s", task_id)

        return {"task_id": task_id, "status": "evaluating"}

    except FMEABaseException as e:
        logger.error("full_evaluation_v2 业务异常: %s", e)
        _release_task_lock(task_id)
        update_task(task_id, status="failure", error_message=e.message,
                    result={"error_code": e.error_code, "detail": e.message})
        return {"task_id": task_id, "status": "business_error"}

    except Exception as e:
        logger.error("full_evaluation_v2 系统异常: %s", traceback.format_exc())
        _release_task_lock(task_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        update_task(task_id, status="failure", error_message=str(e),
                    result={"error_code": ErrorCode.SYSTEM_ERROR, "detail": str(e)})
        return {"task_id": task_id, "status": "system_error"}


@celery_app.task(bind=True, max_retries=2)
def evaluate_single_defect(self, task_id: int, defect: dict):
    """评估单个缺陷（无状态，线程安全）。"""
    try:
        heartbeat(task_id)
        evaluated = evaluate_one_defect(defect)
        # 防止评估过程意外丢弃 type
        return _normalize_defect_type(evaluated)
    except Exception as e:
        logger.error("评估缺陷 %s 失败: %s", defect.get("id", ""), e)
        # 保留原始缺陷信息，标注错误
        return {
            **_normalize_defect_type(defect),
            "error": str(e),
            "severity": 0,
            "occurrence": 0,
            "detection": 0,
            "rpn": 0,
            "risk_level": "评估失败",
            "reasons": [],
        }


@celery_app.task(bind=True)
def review_and_proceed(self, evaluated_results: List[dict], task_id: int):
    """
    chord 回调：收集评估结果，判断是否存在高风险缺陷。
    若存在高风险 (RPN > 150)，挂起任务等待人工审核；
    否则直接进入法规审计阶段。
    """
    logger.info("收到 %d 条评估结果，任务 %s", len(evaluated_results), task_id)

    # ── 二次防丢：规范化所有缺陷的 type ──
    evaluated_results = [_normalize_defect_type(d) for d in evaluated_results]

    # 过滤掉评估失败的缺陷（可选处理）
    valid = [d for d in evaluated_results if d.get("rpn", 0) > 0 or d.get("error") is not None]
    high_risk = [d for d in valid if d.get("rpn", 0) > 150]

    if high_risk:
        # 保存中间结果，改变状态为 pending_review
        update_task(
            task_id,
            status="pending_review",
            progress=50,
            result={
                "evaluated": evaluated_results,
                "high_risk_ids": [d.get("id") for d in high_risk],
            },
        )
        logger.info("任务 %s 进入待审核状态，高风险缺陷 %d 条", task_id, len(high_risk))
        return "review_required"
    else:
        # 无高风险，直接继续审计
        return start_audit_phase(task_id, evaluated_results)


def start_audit_phase(task_id: int, evaluated_defects: List[dict]):
    """
    启动法规审计阶段（并行），会在 Celery worker 上下文执行。
    """
    update_task(task_id, status="started", progress=60)

    # ── 三次防丢：确保进入审计的缺陷都有 type ──
    evaluated_defects = [_normalize_defect_type(d) for d in evaluated_defects]

    audit_jobs = group(
        audit_single_defect.s(task_id, defect)
        for defect in evaluated_defects
    )
    # 审计完成后最后汇总
    (audit_jobs | finalize_full_evaluation_v2.s(task_id)).apply_async()
    return "audit_started"


@celery_app.task(bind=True, max_retries=2)
def audit_single_defect(self, task_id: int, defect: dict):
    """对单个缺陷执行法规检索（纯 Python，可并行），内置 type 保底逻辑。"""
    try:
        heartbeat(task_id)
        # 最后一次防丢＋审计
        defect = _normalize_defect_type(defect)
        audited = audit_one_defect(defect)
        # 若 audit_one_defect 返回的对象不包含 type（极端情况），重新补上
        return _normalize_defect_type(audited)
    except Exception as e:
        logger.error("法规审计缺陷 %s 失败: %s", defect.get("id", ""), e)
        # 审计失败也要保留 type，避免后续流程出错
        fallback = {**_normalize_defect_type(defect), "error": str(e)}
        return fallback


@celery_app.task(bind=True)
def finalize_full_evaluation_v2(self, audited_defects: List[dict], task_id: int):
    """最终汇总，标记任务成功。"""
    try:
        full_result = {
            "report_summary": f"共评估 {len(audited_defects)} 条缺陷",
            "defects": audited_defects,
        }
        update_task(
            task_id,
            status="success",
            progress=100,
            result=full_result,
            completed_at=datetime.datetime.utcnow(),
        )
        logger.info("任务 %s 完成", task_id)
        return full_result
    except Exception as e:
        logger.error("任务 %s 最终化失败: %s", e)
        update_task(task_id, status="failure", error_message=str(e),
                    result={"error_code": "FINALIZE_ERROR", "detail": str(e)})
        raise


# ================== 人机协同：审核后继续 ==================
@celery_app.task(bind=True, max_retries=1)
def continue_after_review(self, task_id: int, decisions: dict = None):
    """
    人工审核提交后，继续执行法规审计阶段。
    decisions: 例如 {"1": {"action": "accept", "comment": ""}, "2": {"action": "reject", "comment": ""}}
    """
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task or task.status != "pending_review":
            logger.error("任务 %s 状态不正确 (expected pending_review)", task_id)
            return "invalid_status"

        stored = task.result or {}
        evaluated = stored.get("evaluated", [])
        if not evaluated:
            return "no_evaluated_data"

        # 应用审核决策：移除被 reject 的缺陷，或标记
        if decisions:
            for defect in evaluated:
                did = str(defect.get("id"))
                if did in decisions:
                    d = decisions[did]
                    defect["review_decision"] = d
                    if d.get("action") == "reject":
                        defect["_remove"] = True

            evaluated = [d for d in evaluated if not d.get("_remove")]

        # ── 防止审核后的缺陷丢失 type ──
        evaluated = [_normalize_defect_type(d) for d in evaluated]

        # 更新数据库中的中间结果
        task.result = {"evaluated": evaluated}
        db.commit()

    # 启动审计阶段
    start_audit_phase(task_id, evaluated)
    return "audit_started"