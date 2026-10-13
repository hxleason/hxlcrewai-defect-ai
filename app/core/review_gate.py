"""
审核闸门统一判定（v9.0）
全系统唯一的高风险/失败/警告判定入口，供异步任务、同步路径、审核路由共用，
杜绝"异步拦截、同步放行"的不一致（评估报告 6.1 / 6.3）。
"""
from app.core.config import settings

UNRATED_LEVELS = ("评估失败", "无法评定")


def needs_review(defect: dict) -> bool:
    """单条缺陷是否需要人工审核，任一条件命中即 True。"""
    if defect.get("error"):
        return True
    if defect.get("persist_error"):
        return True
    if defect.get("risk_level") in UNRATED_LEVELS:
        return True
    if defect.get("warning"):
        return True
    rpn = defect.get("rpn")
    if rpn is not None and isinstance(rpn, (int, float)) and rpn >= settings.HIGH_RISK_THRESHOLD:
        return True
    sev = defect.get("severity")
    if (
        settings.FORCE_SUSPEND_S9
        and sev is not None
        and isinstance(sev, (int, float))
        and sev >= 9
    ):
        return True
    return False


def review_required_ids(defects: list) -> list:
    return [d.get("id") for d in defects if needs_review(d)]