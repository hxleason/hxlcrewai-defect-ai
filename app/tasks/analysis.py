"""
app/tasks/analysis.py – Celery 任务定义（v7.8 · 审核门与一致性统一版）
────────────────────────────────────────────────────────────────
功能：
  - 保留原 v3.1 所有任务，向前兼容
  - full_evaluation_v2（幂等 + 字段防丢 + 故障兜底）
  - 评估失败不再吞没，自动重试直至耗尽后标记错误
  - 评估失败/无法评定/高风险/S≥9/存在警告/持久化失败 一律强制挂起
  - 最终汇总标记 partial_failure，任务不再伪装成功
  - 幂等锁确保 chord 不重复创建
  - 字段防丢 _normalize_defect_type 全链路覆盖
  - 适配 crews.py（返回 Pydantic 对象，自动序列化）

v7.0 更新：
  - 新增 _persist_defect_audit 函数：审计完成后将完整结果（含 pwht_advice）
    持久化到 Defect 表，确保 PWHT 工艺建议可通过缺陷详情接口查询
  - audit_single_defect 成功及失败路径均调用持久化
  - 修复 review_and_proceed / finalize_full_evaluation_v2 中
    "无法评定" 字符串的软连字符（U+00AD）问题，确保风险等级匹配正确
  - 导入 Defect 模型，新增 _find_defect_record 匹配辅助函数

v7.1 更新：
  - 新增 _propagate_wall_thickness：同一报告内缺陷壁厚自动传播。
  - full_evaluation_v2 在缺陷提取后、任务分发前调用壁厚传播。
  - continue_after_review 中同样调用壁厚传播。

v7.2 更新：
  - 修复人工审核缺陷 ID 匹配失败的严重 Bug。
    在 review_and_proceed 中为所有缺陷分配临时顺序 ID（1,2,3...），
    确保 API 端提交的 defect_id 能与后端缺陷成功对应。

v7.3 更新：
  - 新增 _create_defect_records：缺陷提取后立即写入 Defect 表，
    解决 _persist_defect_audit 找不到匹配记录的问题。
  - _persist_defect_audit 增加兜底逻辑：找不到记录时自动创建。
  - 新增 _safe_set_attribute、_create_defect_record_from_dict 辅助函数。

v7.4 更新：
  - evaluate_single_defect 评估完成后立即调用 _persist_defect_audit，
    确保高风险缺陷即使被 review_and_proceed 强制挂起，也能在 Defect 表
    中查询到 FMEA 评分、风险等级及 PWHT 修复工艺建议。
    失败路径同样持久化，保持数据库状态一致。

v7.5 更新：
  - 修复 review_and_proceed 中人工审核挂起阈值硬编码问题：
    从 `rpn > 150` 改为 `rpn >= settings.HIGH_RISK_THRESHOLD`。

v7.6 更新（P0 修复）：
  - 修复人工审核状态机 Bug：continue_after_review 现同时接受
    `pending_review` 和 `processing` 两种状态。

v7.7 更新：
  - 修正 review_and_proceed 日志与 docstring 的风险描述，
    使日志文案与实际判断逻辑保持一致。

v7.8 更新（本次 · 审核门与一致性）：
  - 引入统一审核闸门 app/core/review_gate.needs_review：
    review_and_proceed / continue_after_review / finalize 均走同一判定，
    S≥9 强制挂起（FORCE_SUSPEND_S9）在此正式生效（评估报告 6.1）。
  - 壁厚传播 / 临时 ID 分配迁移至公共模块 app/core/defect_utils.py，
    删除本地重复实现，统一为"唯一有效壁厚值才传播"（评估报告 6.3）。
  - 幂等锁改为原子 SET NX EX + token + Lua 校验释放（评估报告 6.7）。
  - _persist_defect_audit 返回 bool，失败写入 persist_error，
    finalize 阶段计入 partial_failure（评估报告 6.2）。
  - continue_after_review 校验审核决策完整性，并记录 review_audit。
  - finalize 结果新增 meta 运行元信息（模型 / prompt 版本 / 规则哈希）。
"""

import datetime
import hashlib
import logging
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import redis
from celery import group, chord
from celery.exceptions import SoftTimeLimitExceeded

# 自动适配 celery_app 位置
try:
    from app.celery_app import celery_app
except ModuleNotFoundError:
    from app.tasks.celery_app import celery_app

from app.db.database import SessionLocal
from app.models import Task, Defect          # ★ v7.0 新增导入 Defect
from app.core.config import settings
# 修正导入路径，确保使用 crews.py
from app.crews import (
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

# ★ v7.8：统一审核闸门与缺陷工具（壁厚传播 / 临时 ID 唯一实现）
from app.core.review_gate import needs_review
from app.core.audit import record_assessment
from app.core.defect_utils import (
    propagate_wall_thickness as _propagate_wall_thickness,
    assign_temp_defect_ids as _assign_temp_defect_ids,
)

# Redis 客户端（幂等锁）
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

logger = logging.getLogger(__name__)

LOCK_PREFIX = "fmea:task:lock:"
LOCK_TTL = 3600


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


# ================== 幂等性工具（v7.8 原子锁） ==================
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


def _acquire_task_lock(task_id: int) -> bool:
    """以原子方式加锁：SET key token NX EX ttl。返回是否获取成功。"""
    key = f"{LOCK_PREFIX}{task_id}"
    token = uuid.uuid4().hex
    if redis_client.set(key, token, nx=True, ex=LOCK_TTL):
        redis_client.set(f"{key}:token", token, ex=LOCK_TTL)
        return True
    return False


def _release_task_lock(task_id: int):
    """仅锁持有者（token 匹配）可释放，旧执行者无法误删新锁。"""
    key = f"{LOCK_PREFIX}{task_id}"
    token = redis_client.get(f"{key}:token")
    if not token:
        return
    redis_client.eval(_RELEASE_LUA, 1, key, token)
    redis_client.delete(f"{key}:token")


# ================== 字段修复 ==================
KNOWN_DEFECT_TYPES = [
    "裂纹", "咬边", "气孔", "未熔合", "未焊透",
    "夹渣", "变形", "腐蚀", "磨损", "凹坑",
    "鼓包", "错边", "焊瘤", "飞溅", "渗漏"
]


def _normalize_defect_type(defect: dict) -> dict:
    """
    确保缺陷字典中存在非空的 type 字段。
    若缺失，则尝试从 original_text 或 location 中提取已知关键词。
    如果仍然无法识别，则置为"未知缺陷"并记录告警。
    """
    if defect.get("type"):
        return defect

    text = f"{defect.get('original_text','')} {defect.get('location','')}".lower()
    for keyword in KNOWN_DEFECT_TYPES:
        if keyword in text:
            defect["type"] = keyword
            logger.warning("补充缺陷 %s 的 type = '%s'（从文本推断）", defect.get("id"), keyword)
            return defect

    defect["type"] = "未知缺陷"
    logger.warning("缺陷 %s 的 type 缺失且无法自动推断，已设为 '未知缺陷'", defect.get("id"))
    return defect


# ─────────────────────────────────────────────────────────
# ★ v7.8：壁厚传播 / 临时 ID 分配已迁移至 app/core/defect_utils.py
#   本文件通过顶部 import 复用唯一实现，不再保留本地重复定义。
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# ★ v7.3 新增：缺陷记录创建与安全属性设置
# ─────────────────────────────────────────────────────────

def _safe_set_attribute(obj: Any, attr: str, value: Any) -> None:
    """仅在模型存在该属性时赋值，避免字段不匹配报错。"""
    if hasattr(obj, attr):
        setattr(obj, attr, value)


def _create_defect_record_from_dict(db, task_id: int, defect: dict) -> Defect:
    """
    在给定的数据库 session 内，根据缺陷字典创建 Defect 实例（不提交）。
    使用 _safe_set_attribute 确保仅设置模型存在的字段。
    """
    db_defect = Defect()
    _safe_set_attribute(db_defect, "task_id", task_id)
    _safe_set_attribute(db_defect, "defect_type", defect.get("type") or defect.get("defect_type"))
    _safe_set_attribute(db_defect, "original_text", (defect.get("original_text") or "").strip())
    _safe_set_attribute(db_defect, "component", defect.get("component"))
    _safe_set_attribute(db_defect, "location", defect.get("location"))
    _safe_set_attribute(db_defect, "depth", defect.get("depth"))
    _safe_set_attribute(db_defect, "length", defect.get("length"))
    _safe_set_attribute(db_defect, "wall_thickness", defect.get("wall_thickness"))
    _safe_set_attribute(db_defect, "unit", defect.get("unit", "mm"))
    _safe_set_attribute(db_defect, "quantity", defect.get("quantity", 1))
    _safe_set_attribute(db_defect, "media", defect.get("media"))
    _safe_set_attribute(db_defect, "material", defect.get("material"))
    _safe_set_attribute(db_defect, "device_type", defect.get("device_type"))
    _safe_set_attribute(db_defect, "environment", defect.get("environment"))
    _safe_set_attribute(db_defect, "operating_temperature", defect.get("operating_temperature"))
    _safe_set_attribute(db_defect, "design_pressure", defect.get("design_pressure"))
    _safe_set_attribute(db_defect, "detection_method", defect.get("detection_method"))
    _safe_set_attribute(db_defect, "service_years", defect.get("service_years"))
    _safe_set_attribute(db_defect, "inspection_interval", defect.get("inspection_interval"))
    return db_defect


def _create_defect_records(task_id: int, defects: List[dict]) -> int:
    """
    ★ v7.3 新增：缺陷提取后立即将缺陷写入 Defect 表（幂等）。

    幂等策略：
      每条缺陷的 original_text 在报告中唯一，创建前检查
      task_id + original_text 是否已存在，存在则跳过。

    返回实际创建的记录数，任何异常只记录日志，不中断主流程。
    """
    created_count = 0
    try:
        with SessionLocal() as db:
            for defect in defects:
                if not isinstance(defect, dict):
                    continue

                original_text = (defect.get("original_text") or "").strip()
                if not original_text:
                    logger.warning("缺陷缺少 original_text，无法入库 (task_id=%s)", task_id)
                    continue

                # 幂等检查：同任务同原文已存在则跳过
                exists = db.query(Defect).filter(
                    Defect.task_id == task_id,
                    Defect.original_text == original_text,
                ).first()
                if exists:
                    logger.debug("缺陷记录已存在，跳过创建: %s", original_text[:40])
                    continue

                db_defect = _create_defect_record_from_dict(db, task_id, defect)
                db.add(db_defect)
                created_count += 1

            db.commit()
            if created_count:
                logger.info("缺陷入库完成：新创建 %d 条记录 (task_id=%s)", created_count, task_id)
            else:
                logger.info("缺陷记录均已存在，无需创建 (task_id=%s)", task_id)

    except Exception as e:
        logger.error("缺陷入库失败 task_id=%s: %s", task_id, e)
        # 关键：入库失败不能阻断主流程，后续 _persist_defect_audit 仍有兜底逻辑

    return created_count


# ─────────────────────────────────────────────────────────
# ★ v7.0 新增：缺陷审计结果持久化（含 v7.3 兜底；v7.8 返回 bool）
# ─────────────────────────────────────────────────────────

def _find_defect_record(db, task_id: int, defect: dict):
    """
    在数据库中查找匹配的缺陷记录。

    匹配策略（按优先级降序）：
      1. task_id + original_text（最可靠）
      2. task_id + defect_type + component + location（组合匹配）
      3. task_id + defect_type 且仅有一条记录（唯一类型）
      4. 无法匹配返回 None
    """
    # --- 策略 1：task_id + original_text ---
    original_text = defect.get("original_text", "")
    if original_text:
        record = db.query(Defect).filter(
            Defect.task_id == task_id,
            Defect.original_text == original_text,
        ).first()
        if record:
            return record

    # --- 策略 2：task_id + defect_type + component + location ---
    defect_type = defect.get("type") or defect.get("defect_type") or ""
    component = defect.get("component")
    location = defect.get("location")

    query = db.query(Defect).filter(
        Defect.task_id == task_id,
        Defect.defect_type == defect_type,
    )
    if component:
        query = query.filter(Defect.component == component)
    if location:
        query = query.filter(Defect.location == location)

    record = query.first()
    if record:
        return record

    # --- 策略 3：task_id + defect_type 且仅有一条记录 ---
    records = db.query(Defect).filter(
        Defect.task_id == task_id,
        Defect.defect_type == defect_type,
    ).all()
    if len(records) == 1:
        return records[0]

    # --- 无法匹配 ---
    logger.warning(
        "无法在数据库中找到匹配的缺陷记录 task_id=%s defect_id=%s type=%s",
        task_id, defect.get("id"), defect_type
    )
    return None


def _persist_defect_audit(task_id: int, defect: dict) -> bool:
    """
    将审计后的缺陷完整信息（含 FMEA 评估结果、法规信息及 PWHT 建议）
    持久化到 Defect 表。

    ★ v7.3 更新：找不到匹配记录时自动创建一条新记录后再更新。
    ★ v7.8 更新：返回 bool，失败不再被静默吞掉，调用方写入 persist_error。

    返回 True 表示持久化成功，False 表示失败（不会抛出异常）。
    """
    try:
        with SessionLocal() as db:
            db_defect = _find_defect_record(db, task_id, defect)
            if not db_defect:
                # ★ v7.3 兜底：找不到记录时自动创建
                original_text = (defect.get("original_text") or "").strip()
                if not original_text:
                    logger.warning(
                        "持久化缺陷审计结果时未找到匹配记录且缺少 original_text，跳过 task_id=%s defect_id=%s",
                        task_id, defect.get("id")
                    )
                    return False

                logger.info(
                    "未找到缺陷记录，自动创建新记录 task_id=%s defect_id=%s type=%s",
                    task_id, defect.get("id"), defect.get("type")
                )
                db_defect = _create_defect_record_from_dict(db, task_id, defect)
                db.add(db_defect)
                db.flush()  # 获取自增 ID，但不提交，后续统一提交

            # --- FMEA 评估字段 ---
            if defect.get("severity") is not None:
                db_defect.severity = defect.get("severity")
            if defect.get("occurrence") is not None:
                db_defect.occurrence = defect.get("occurrence")
            if defect.get("detection") is not None:
                db_defect.detection = defect.get("detection")
            if defect.get("rpn") is not None:
                db_defect.rpn = defect.get("rpn")
            if defect.get("risk_level") is not None:
                db_defect.risk_level = defect.get("risk_level")
            if defect.get("level") is not None:
                db_defect.level = defect.get("level")
            if defect.get("reasons") is not None:
                db_defect.reasons = defect.get("reasons")
            if defect.get("suggestion") is not None:
                db_defect.suggestion = defect.get("suggestion")
            if defect.get("standard_ref") is not None:
                db_defect.standard_ref = defect.get("standard_ref")
            if defect.get("triggered_rules") is not None:
                db_defect.triggered_rules = defect.get("triggered_rules")

            # --- 法规审核字段 ---
            if defect.get("law_references") is not None:
                db_defect.law_references = defect.get("law_references")
            if defect.get("mandatory_measures") is not None:
                db_defect.mandatory_measures = defect.get("mandatory_measures")
            if defect.get("inspection_advice") is not None:
                db_defect.inspection_advice = defect.get("inspection_advice")

            # --- PWHT 修复工艺建议 ---
            if defect.get("pwht_advice") is not None:
                db_defect.pwht_advice = defect.get("pwht_advice")

            # --- 扩展字段（warning 等额外信息合并入 extra_data） ---
            extra = dict(db_defect.extra_data or {})
            for key in ("warning", "rule_applications", "similar_cases",
                        "similar_case_ids", "similar_case_measures",
                        "review_decision"):
                if defect.get(key) is not None:
                    extra[key] = defect[key]
            if extra:
                db_defect.extra_data = extra

            db.commit()
            logger.info(
                "缺陷 %s (task_id=%s) 审计结果已持久化，pwht_advice=%s",
                defect.get("id"), task_id,
                "已写入" if defect.get("pwht_advice") else "未生成"
            )

            return True

    except Exception as e:
        logger.error(
            "持久化缺陷审计结果失败 task_id=%s defect_id=%s: %s",
            task_id, defect.get("id"), e
        )
        return False


# ================== 旧版任务（保留不动，兼容性修复） ==================
@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    time_limit=600,
    soft_time_limit=540,
)
def analysis_task(self, task_id: int, input_text: str) -> Dict[str, Any]:
    """仅缺陷提取（解析 Agent）—— 原版保留，适配新版 crews 返回 Pydantic 对象"""
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
        # 新版 crews 可能返回 Pydantic 对象，需转为纯字典后再存储
        if hasattr(raw_result, 'model_dump'):
            raw_result = raw_result.model_dump()
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
        result_dict = run_fmea_evaluation(input_text)   # crews 兼容函数，返回 dict
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
        result_dict = run_full_fmea_evaluation(input_text)   # crews 兼容函数，返回 dict
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


# ================== 新版高效全流程任务（v2，终极安全版） ==================

@celery_app.task(bind=True, max_retries=2)
def full_evaluation_v2(self, task_id: int, input_text: str):
    """
    终极版完整评估（幂等 + 字段防丢 + 故障挂起）：
    提取 → 壁厚传播 → 提前入库 → 并行评级 → 强制挂起（含失败/警告/S9） → 并行法规 → 汇总

    ★ v7.3 新增：评估前调用 _create_defect_records 将缺陷写入数据库，
      确保审计完成后 _persist_defect_audit 能找到匹配记录。

    ★ v7.4 新增：evaluate_single_defect 内也会主动持久化评估结果，
      确保高风险缺陷在挂起前已有完整数据。

    ★ v7.8 更新：挂起判定统一走 app.core.review_gate.needs_review。
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
        defects = extract_defects(input_text)          # 内部调用新版 crews，返回字典列表
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

        # 3. 壁厚自动传播（唯一有效值才传播，v7.8 公共实现）
        defects = _propagate_wall_thickness(defects)

        # 4. 为缺陷分配临时 ID（用于后续人工审核）
        defects = _assign_temp_defect_ids(defects)

        # 5. 缺陷提取后立即入库，确保审计后持久化能找到匹配记录
        _create_defect_records(task_id, defects)

        update_task(task_id, progress=20)

        # 6. 并行评估所有缺陷（纯 Python，可自动重试）
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
    """
    评估单个缺陷（支持自动重试）。

    - 正常返回包含 FMEA 结果的完整缺陷字典。
    - 异常时 Celery 自动重试（最多 max_retries 次）；
    - 重试耗尽后，返回带有 `error` 字段的结果，不再吞没异常。

    ★ v7.8 更新：持久化失败写入 persist_error，由统一审核闸门强制挂起。
    """
    try:
        heartbeat(task_id)
        evaluated = evaluate_one_defect(defect)
        evaluated = _normalize_defect_type(evaluated)

        # 评估结果立即持久化（含 pwht_advice）
        ok = _persist_defect_audit(task_id, evaluated)
        if not ok:
            evaluated["persist_error"] = "评估结果持久化失败，已标记人工复核"

        return evaluated

    except Exception as e:
        logger.error("评估缺陷 %s 失败: %s", defect.get("id"), e)
        if self.request.retries < self.max_retries:
            logger.info("准备重试评估缺陷 %s", defect.get("id"))
            raise self.retry(exc=e)

        # 重试耗尽：返回错误标记，供 review_and_proceed 强制挂起
        logger.warning("缺陷 %s 评估重试耗尽，返回错误标记", defect.get("id"))
        error_defect = {
            **_normalize_defect_type(defect),
            "error": f"评估失败（已重试 {self.max_retries} 次）: {e}",
            "severity": None,
            "occurrence": None,
            "detection": None,
            "rpn": None,
            "risk_level": "评估失败",
            "reasons": [],
        }

        # 失败结果也持久化，确保数据库状态一致
        ok = _persist_defect_audit(task_id, error_defect)
        if not ok:
            error_defect["persist_error"] = "失败结果持久化失败，已标记人工复核"

        return error_defect


@celery_app.task(bind=True)
def review_and_proceed(self, evaluated_results: List[dict], task_id: int):
    """
    chord 回调：接收所有评估结果，通过统一审核闸门决定挂起或进入审计。

    挂起条件（统一由 needs_review 判定）：
    - 含有 error 字段（评估异常）
    - 含有 persist_error（持久化异常）
    - risk_level == "评估失败" 或 "无法评定"
    - RPN >= settings.HIGH_RISK_THRESHOLD
    - 存在 warning 字段且非空（例如壁厚缺失警告）
    - FORCE_SUSPEND_S9 且 severity >= 9

    仅当全部缺陷均无需人工审核时，才直接进入法规审计阶段。
    """
    heartbeat(task_id)
    logger.info("收到 %d 条评估结果，任务 %s", len(evaluated_results), task_id)

    # 字段防丢
    evaluated_results = [_normalize_defect_type(d) for d in evaluated_results]

    # 确保每条缺陷都有 ID（评估阶段可能丢失）
    evaluated_results = _assign_temp_defect_ids(evaluated_results)

    # 统一审核闸门判定
    require_review = [d for d in evaluated_results if needs_review(d)]
    ok_for_audit = [d for d in evaluated_results if not needs_review(d)]

    if require_review:
        logger.info(
            "任务 %s 进入待审核状态，需审核缺陷 %d 条（高风险/S9/失败/警告/持久化异常）",
            task_id, len(require_review),
        )
        update_task(
            task_id,
            status="pending_review",
            progress=50,
            result={
                "evaluated": evaluated_results,
                "high_risk_or_error_ids": [d.get("id") for d in require_review],
                "pending_reason": "存在高风险(RPN≥阈值)、S≥9、评估失败、无法评定、警告或持久化异常，需人工介入",
            },
        )
        return "review_required"
    else:
        logger.info(
            "所有缺陷均无需人工审核（无评估失败/无法评定/错误/警告，且 RPN、S 未超过挂起阈值），进入法规审计，任务 %s",
            task_id,
        )
        start_audit_phase(task_id, ok_for_audit)
        return "audit_started"


def start_audit_phase(task_id: int, evaluated_defects: List[dict]):
    """启动法规审计阶段（并行），并在完成后最终汇总。"""
    heartbeat(task_id)
    update_task(task_id, status="started", progress=60)

    # 防丢
    evaluated_defects = [_normalize_defect_type(d) for d in evaluated_defects]

    audit_jobs = group(
        audit_single_defect.s(task_id, defect)
        for defect in evaluated_defects
    )
    # 审计完成后回调 finalize_full_evaluation_v2
    (audit_jobs | finalize_full_evaluation_v2.s(task_id)).apply_async()
    logger.info("审计阶段已启动，任务 %s", task_id)


@celery_app.task(bind=True, max_retries=2)
def audit_single_defect(self, task_id: int, defect: dict):
    """
    对单个缺陷执行法规检索（纯 Python，可并行）。

    ★ v7.0 更新：审计成功后持久化完整结果（含 pwht_advice）；
      审计失败（重试耗尽）时同样持久化错误状态，确保数据库记录一致。
    ★ v7.8 更新：持久化失败写入 persist_error，由统一审核闸门强制挂起。
    """
    try:
        heartbeat(task_id)
        defect = _normalize_defect_type(defect)
        audited = audit_one_defect(defect)
        record_assessment(defect, audited)
        audited = _normalize_defect_type(audited)

        # 持久化审计结果（含 pwht_advice）
        ok = _persist_defect_audit(task_id, audited)
        if not ok:
            audited["persist_error"] = "审计结果持久化失败，已标记人工复核"

        return audited

    except Exception as e:
        logger.error("法规审计缺陷 %s 失败: %s", defect.get("id"), e)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        # 重试耗尽后仍返回错误标记
        error_defect = {
            **_normalize_defect_type(defect),
            "error": f"法规审计失败: {e}",
            "law_references": "",
            "mandatory_measures": "",
            "inspection_advice": "",
        }
        # 失败时也持久化，确保数据库状态一致
        ok = _persist_defect_audit(task_id, error_defect)
        if not ok:
            error_defect["persist_error"] = "失败结果持久化失败，已标记人工复核"
        return error_defect


@celery_app.task(bind=True)
def finalize_full_evaluation_v2(self, audited_defects: List[dict], task_id: int):
    """
    最终汇总回调：标记任务完成或部分失败，释放幂等锁。

    - 若存在审计失败、评估失败、无法评定或持久化失败，任务状态设为 `partial_failure`。
    - 否则标记为 `success`。
    - 结果附带 meta 运行元信息（模型 / prompt 版本 / 规则哈希）。
    """
    heartbeat(task_id)
    try:
        # 统计错误/失败缺陷（含持久化失败）
        failed_defects = [
            d for d in audited_defects
            if d.get("error")
            or d.get("risk_level") in ("评估失败", "无法评定")
            or d.get("persist_error")
        ]
        has_failures = len(failed_defects) > 0

        summary = f"共评估 {len(audited_defects)} 条缺陷"
        if has_failures:
            summary += f"，其中 {len(failed_defects)} 条处理失败（含持久化失败），请人工介入"
        else:
            summary += "，全部处理成功且已落库"

        full_result = {
            "report_summary": summary,
            "defects": audited_defects,
            "failed_count": len(failed_defects),
            "failed_defect_ids": [d.get("id") for d in failed_defects],
            "meta": _build_run_meta(),
        }

        final_status = "partial_failure" if has_failures else "success"
        update_task(
            task_id,
            status=final_status,
            progress=100,
            result=full_result,
            completed_at=datetime.datetime.utcnow(),
        )
        logger.info("任务 %s 完成（状态：%s）", task_id, final_status)
        return full_result

    except Exception as e:
        logger.error("任务 %s 最终化失败: %s", task_id, e)
        update_task(task_id, status="failure", error_message=str(e),
                    result={"error_code": "FINALIZE_ERROR", "detail": str(e)})
        raise
    finally:
        _release_task_lock(task_id)          # 无论成败都释放幂等锁


# ================== 人机协同：审核后继续 ==================
@celery_app.task(bind=True, max_retries=1)
def continue_after_review(self, task_id: int, decisions: dict = None):
    """
    人工审核提交后，继续执行法规审计阶段。
    decisions: {"1": {"action": "accept", "comment": ""}, "2": {"action": "reject", ...}}

    ★ v7.6 P0 修复：同时接受 pending_review 和 processing 两种状态。

    ★ v7.8 更新：
      - 审核决策必须覆盖全部待审核缺陷，否则拒绝继续并恢复 pending_review；
      - 记录 review_audit（审核人 / 时间 / 决策）。
    """
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task or task.status not in ("pending_review", "processing"):
            logger.error(
                "任务 %s 状态不正确 (expected pending_review or processing, got %s)",
                task_id, task.status if task else None,
            )
            return "invalid_status"

        stored = task.result or {}
        evaluated = stored.get("evaluated", [])
        if not evaluated:
            return "no_evaluated_data"

        decisions = decisions or {}
        # 统一判定需审核缺陷（含 S9），决策必须全覆盖，否则拒绝继续
        pending_ids = {str(d.get("id")) for d in evaluated if needs_review(d)}
        missing = pending_ids - set(decisions.keys())
        if missing:
            task.status = "pending_review"  # 恢复待审核状态
            db.commit()
            logger.error("任务 %s 审核决策不完整，缺少缺陷: %s", task_id, sorted(missing))
            return "incomplete_decisions"

        # 应用审核决策
        if decisions:
            for defect in evaluated:
                did = str(defect.get("id"))
                if did in decisions:
                    d = decisions[did]
                    defect["review_decision"] = d
                    if d.get("action") == "reject":
                        defect["_remove"] = True
            evaluated = [d for d in evaluated if not d.get("_remove")]

        # 防丢
        evaluated = [_normalize_defect_type(d) for d in evaluated]

        # 审核后同样执行壁厚传播（防止移除缺陷导致原传播链路失效）
        evaluated = _propagate_wall_thickness(evaluated)

        # 再次确保 ID 存在
        evaluated = _assign_temp_defect_ids(evaluated)

        # 更新中间结果 + 审核审计
        task.result = {
            "evaluated": evaluated,
            "review_audit": {
                "reviewer": decisions.get("_reviewer", "unknown"),
                "reviewed_at": datetime.datetime.utcnow().isoformat(),
                "decisions": {k: v for k, v in decisions.items() if not k.startswith("_")},
            },
        }
        db.commit()

    heartbeat(task_id)
    start_audit_phase(task_id, evaluated)
    return "audit_started"


# ================== 运行元信息 ==================
def _file_md5(path) -> str:
    """读取文件并返回 MD5 前 12 位；失败返回 'n/a'。"""
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()[:12]
    except Exception:
        return "n/a"


def _build_run_meta() -> dict:
    """构建本次评估的运行元信息（模型 / prompt 版本 / 规则哈希）。"""
    from app.core.config import RULES_PATH, PROJECT_ROOT

    expert = PROJECT_ROOT / "data" / "expert_rules_v2.json"
    if not expert.exists():
        expert = PROJECT_ROOT / "data" / "expert_rules.json"

    return {
        "model": settings.LLM_MODEL,
        "prompt_version": "v8.3",
        "rules_hash": _file_md5(RULES_PATH),
        "expert_rules_hash": _file_md5(expert),
    }