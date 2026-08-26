"""
app/tasks/analysis.py – Celery 任务定义（终极版 v7.5）
────────────────────────────────────────────────────────────────
功能：
  - 保留原 v3.1 所有任务，向前兼容
  - full_evaluation_v2（幂等 + 字段防丢 + 故障兜底）
  - 评估失败不再吞没，自动重试直至耗尽后标记错误
  - 评估失败/无法评定/高风险/存在警告（壁厚缺失）一律强制挂起
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
    当某缺陷缺少 wall_thickness 而其他缺陷有值时，取最小有效壁厚
    （保守原则）自动补充，避免因个别缺陷（如接管角焊缝）未单独
    标注壁厚而触发不必要的强制人工审核。
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
  - 新增 _safe_set_attribute、_create_defect_record_from_dict 辅助函数，
    防止模型字段不匹配报错，并减少代码重复。

v7.4 更新（本次）：
  - evaluate_single_defect 在评估完成后立即调用 _persist_defect_audit，
    确保高风险缺陷即使被 review_and_proceed 强制挂起，也能在 Defect 表
    中查询到 FMEA 评分、风险等级及 PWHT 修复工艺建议。
    失败路径同样持久化，保持数据库状态一致。

v7.5 更新：
  - 修复 review_and_proceed 中人工审核挂起阈值硬编码问题：
    从 `rpn > 150` 改为 `rpn >= settings.HIGH_RISK_THRESHOLD`，
    与配置及 crews.py 保持一致。
"""

import datetime
import re
import traceback
import logging
from typing import Any, Dict, List, Optional

import redis
from celery import group, chain, chord
from celery.exceptions import SoftTimeLimitExceeded, MaxRetriesExceededError

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

# Redis 客户端（幂等锁）
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

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


# ================== 幂等性工具 ==================
def _acquire_task_lock(task_id: int) -> bool:
    key = f"{LOCK_PREFIX}{task_id}"
    if redis_client.setnx(key, "1") == 0:
        return False
    redis_client.expire(key, LOCK_EXPIRE)
    return True


def _release_task_lock(task_id: int):
    key = f"{LOCK_PREFIX}{task_id}"
    redis_client.delete(key)


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
# ★ v7.1 新增：同一报告内缺陷壁厚自动传播
# ─────────────────────────────────────────────────────────

def _coerce_positive_float(value: Any) -> Optional[float]:
    """
    安全地将值转换为正 float，失败则返回 None。

    支持 int / float / 数字字符串（如 "18"、"18.5"、'18mm'）。
    """
    if value is None:
        return None
    if isinstance(value, bool):          # 排除 True/False
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        # 尝试提取第一个数字（支持 "18" / "18.5" / "18mm" 等）
        m = re.search(r"(\d+(?:\.\d+)?)", value.strip())
        if m:
            try:
                f = float(m.group(1))
                return f if f > 0 else None
            except ValueError:
                return None
    return None


def _propagate_wall_thickness(defects: List[dict]) -> List[dict]:
    """
    同一报告内缺陷壁厚自动传播（保守原则）。

    规则：
      - 收集列表中所有有效 wall_thickness（正数）；
      - 若存在有效值，取最小值（保守）作为推断壁厚；
      - 为缺失 wall_thickness 的缺陷自动补充该值；
      - 若列表中所有缺陷均无有效壁厚，则不修改任何数据。
    """
    if not defects:
        return defects

    valid_thicknesses: List[float] = []
    for d in defects:
        if not isinstance(d, dict):
            continue
        t = _coerce_positive_float(d.get("wall_thickness"))
        if t is not None:
            valid_thicknesses.append(t)

    if not valid_thicknesses:
        logger.warning("缺陷列表 %d 条中无任何有效壁厚，无法自动传播", len(defects))
        return defects

    inferred = min(valid_thicknesses)
    filled_count = 0

    for d in defects:
        if not isinstance(d, dict):
            continue
        existing = _coerce_positive_float(d.get("wall_thickness"))
        if existing is None:
            d["wall_thickness"] = inferred
            filled_count += 1
            logger.info(
                "缺陷 %s (type=%s) 缺少壁厚，已自动补充 %.2f mm（同报告最小有效壁厚，保守值）",
                d.get("id"), d.get("type") or d.get("defect_type"), inferred
            )

    if filled_count:
        logger.info("壁厚自动传播完成：共补充 %d 条缺陷，推断壁厚 %.2f mm", filled_count, inferred)
    return defects


# ─────────────────────────────────────────────────────────
# ★ v7.2 新增：为缺陷分配临时顺序 ID
# ─────────────────────────────────────────────────────────

def _assign_temp_defect_ids(defects: List[dict]) -> List[dict]:
    """
    为缺失 id 的缺陷分配任务内的临时顺序 ID（1,2,3,...），
    确保前端审核时能够准确指定要操作的缺陷。
    """
    next_id = 1
    for d in defects:
        if not isinstance(d, dict):
            continue
        # 已有有效 id 则保留，并用最大值 + 1 作为后续起点
        existing_id = d.get("id")
        if existing_id is not None:
            try:
                next_id = max(next_id, int(existing_id) + 1)
            except (TypeError, ValueError):
                pass
            continue
        d["id"] = next_id
        next_id += 1
    return defects


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

    为什么需要：
      v7.0 的 _persist_defect_audit 只能更新已存在的 Defect 记录，
      但缺陷提取后从未入库，导致持久化永远找不到匹配记录。
      此函数在评估开始前创建记录，审计完成后即可更新。

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
        # ★ 关键：入库失败不能阻断主流程
        # 后续 _persist_defect_audit 仍有兜底逻辑

    return created_count


# ─────────────────────────────────────────────────────────
# ★ v7.0 新增：缺陷审计结果持久化（含 v7.3 兜底）
# ─────────────────────────────────────────────────────────

def _find_defect_record(db, task_id: int, defect: dict):
    """
    在数据库中查找匹配的缺陷记录。

    匹配策略（按优先级降序）：
      1. task_id + original_text（最可靠，每个缺陷的原始文本通常唯一）
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


def _persist_defect_audit(task_id: int, defect: dict) -> None:
    """
    将审计后的缺陷完整信息（含 FMEA 评估结果、法规信息及 PWHT 建议）
    持久化到 Defect 表。

    ★ v7.0 新增：除原有评估/法规字段外，额外持久化 pwht_advice 字段。
    ★ v7.3 更新：当找不到匹配记录时，自动创建一条新记录后再更新，
        避免因缺陷未预先入库而导致持久化失败。

    该函数不抛出异常，任何持久化失败仅记录日志，不影响主任务流程。
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
                    return

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

            # --- ★ v7.0 新增：PWHT 修复工艺建议 ---
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

    except Exception as e:
        logger.error(
            "持久化缺陷审计结果失败 task_id=%s defect_id=%s: %s",
            task_id, defect.get("id"), e
        )


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
    提取 → 壁厚传播 → 提前入库 → 并行评级 → 强制挂起（含失败/警告） → 并行法规 → 汇总

    ★ v7.3 新增：在评估前调用 _create_defect_records 将缺陷写入数据库，
      确保审计完成后 _persist_defect_audit 能找到匹配记录。

    ★ v7.4 新增：evaluate_single_defect 内也会主动持久化评估结果，
      确保高风险缺陷在挂起前已有完整数据。
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

        # 3. ★ v7.1 新增：壁厚自动传播（同一报告内）
        defects = _propagate_wall_thickness(defects)

        # 4. ★ v7.2 新增：为缺陷分配临时 ID（用于后续人工审核）
        defects = _assign_temp_defect_ids(defects)

        # 5. ★ v7.3 新增：缺陷提取后立即入库
        #    确保审计完成后的 _persist_defect_audit 能找到匹配记录
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
    - 若发生异常，Celery 自动重试（最多 max_retries 次）；
    - 重试耗尽后，返回带有 `error` 字段的结果，不再吞没异常。

    ★ v7.4 新增：评估完成后立即调用 _persist_defect_audit 持久化结果，
      确保高风险缺陷即使被 review_and_proceed 强制挂起，也能在 Defect 表
      中查询到 FMEA 评分、风险等级及 PWHT 建议；失败路径同样持久化。
    """
    try:
        heartbeat(task_id)
        evaluated = evaluate_one_defect(defect)
        evaluated = _normalize_defect_type(evaluated)

        # ★ v7.4 核心改动：评估结果立即持久化（含 pwht_advice）
        _persist_defect_audit(task_id, evaluated)

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

        # ★ v7.4 失败结果也持久化，确保数据库状态一致
        _persist_defect_audit(task_id, error_defect)

        return error_defect


@celery_app.task(bind=True)
def review_and_proceed(self, evaluated_results: List[dict], task_id: int):
    """
    chord 回调：接收所有评估结果，强制挂起高风险/失败/无法评定/存在警告的缺陷。

    挂起条件：
    - 含有 error 字段（评估异常）
    - risk_level == "评估失败" 或 "无法评定"
    - RPN >= settings.HIGH_RISK_THRESHOLD  （★ v7.5 修改：原来为 > 150）
    - 存在 warning 字段且非空（例如壁厚缺失警告）

    只有全部缺陷均为有效、无警告且低风险时，才进入法规审计阶段。
    """
    heartbeat(task_id)
    logger.info("收到 %d 条评估结果，任务 %s", len(evaluated_results), task_id)

    # 字段防丢
    evaluated_results = [_normalize_defect_type(d) for d in evaluated_results]

    # ★ v7.2 保险：确保每条缺陷都有 ID（评估阶段可能丢失）
    evaluated_results = _assign_temp_defect_ids(evaluated_results)

    # ── 强制挂起清单 ──
    require_review = []
    ok_for_audit = []
    for defect in evaluated_results:
        has_warning = bool(defect.get("warning"))
        if (
            defect.get("error") or
            defect.get("risk_level") in ("评估失败", "无法评定") or
            (defect.get("rpn") is not None and defect.get("rpn") >= settings.HIGH_RISK_THRESHOLD) or
            has_warning
        ):
            require_review.append(defect)
        else:
            ok_for_audit.append(defect)

    if require_review:
        logger.info("任务 %s 进入待审核状态，需审核缺陷 %d 条（高风险/失败/警告）",
                     task_id, len(require_review))
        update_task(
            task_id,
            status="pending_review",
            progress=50,
            result={
                "evaluated": evaluated_results,
                "high_risk_or_error_ids": [d.get("id") for d in require_review],
                "pending_reason": "存在高风险缺陷、评估失败、无法评定或存在警告（如壁厚缺失），需人工介入",
            },
        )
        return "review_required"
    else:
        logger.info("所有缺陷均为低风险且无警告，直接进入法规审计，任务 %s", task_id)
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

    ★ v7.0 更新：
      - 审计成功后调用 _persist_defect_audit 将完整结果（含 pwht_advice）
        持久化到 Defect 表。
      - 审计失败（重试耗尽）时同样持久化错误状态，确保数据库记录一致。
    """
    try:
        heartbeat(task_id)
        defect = _normalize_defect_type(defect)
        audited = audit_one_defect(defect)
        audited = _normalize_defect_type(audited)

        # ★ v7.0 新增：持久化审计结果（含 pwht_advice）
        _persist_defect_audit(task_id, audited)

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
        # ★ v7.0 新增：失败时也持久化，确保数据库状态一致
        _persist_defect_audit(task_id, error_defect)
        return error_defect


@celery_app.task(bind=True)
def finalize_full_evaluation_v2(self, audited_defects: List[dict], task_id: int):
    """
    最终汇总回调：标记任务完成或部分失败，释放幂等锁。

    - 若存在任何审计失败或评估失败的缺陷，将任务状态设为 `partial_failure`。
    - 否则标记为 `success`。
    """
    heartbeat(task_id)
    try:
        # 统计错误/失败缺陷
        failed_defects = [
            d for d in audited_defects
            if d.get("error") or d.get("risk_level") in ("评估失败", "无法评定")
        ]
        has_failures = len(failed_defects) > 0

        summary = f"共评估 {len(audited_defects)} 条缺陷"
        if has_failures:
            summary += f"，其中 {len(failed_defects)} 条处理失败，请查看详情并人工介入"
        else:
            summary += "，全部处理成功"

        full_result = {
            "report_summary": summary,
            "defects": audited_defects,
            "failed_count": len(failed_defects),
            "failed_defect_ids": [d.get("id") for d in failed_defects],
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

        # ★ v7.1 新增：审核后同样执行壁厚传播（防止审核过程中被移除缺陷导致原传播链路失效）
        evaluated = _propagate_wall_thickness(evaluated)

        # ★ v7.2 保险：再次确保 ID 存在
        evaluated = _assign_temp_defect_ids(evaluated)

        # 更新中间结果
        task.result = {"evaluated": evaluated}
        db.commit()

    heartbeat(task_id)
    start_audit_phase(task_id, evaluated)
    return "audit_started"