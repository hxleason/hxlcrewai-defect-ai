"""
缺陷字典规范化工具（v9.0 唯一实现）
对应评估报告 6.3：删除 crews.py 与 tasks/analysis.py 两处重复实现，
统一为"唯一有效壁厚值才传播"的保守策略。
"""
import logging
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def coerce_positive_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        m = re.search(r"(\d+(?:\.\d+)?)", value.strip())
        if m:
            try:
                f = float(m.group(1))
                return f if f > 0 else None
            except ValueError:
                return None
    return None


def propagate_wall_thickness(defects: List[dict]) -> List[dict]:
    """同一报告内壁厚传播（保守策略）。
    - 收集全部有效壁厚值（归一到 3 位小数）；
    - 仅当唯一时传播；0 个或多个不同值均不传播（禁止跨部件取最小值）；
    - 缺失壁厚保持 None，由 warning → needs_review 强制审核兜底。
    """
    if not defects:
        return defects
    values = set()
    for d in defects:
        if not isinstance(d, dict):
            continue
        t = coerce_positive_float(d.get("wall_thickness"))
        if t is not None:
            values.add(round(t, 3))
    if len(values) != 1:
        return defects
    target = values.pop()
    filled = 0
    for d in defects:
        if isinstance(d, dict) and coerce_positive_float(d.get("wall_thickness")) is None:
            d["wall_thickness"] = target
            filled += 1
    if filled:
        logger.info("壁厚传播（唯一值 %.3f mm）已补充 %d 条缺陷", target, filled)
    return defects


def assign_temp_defect_ids(defects: List[dict]) -> List[dict]:
    """为缺失 id 的缺陷分配任务内临时顺序 ID（1,2,3...）。"""
    next_id = 1
    for d in defects:
        if not isinstance(d, dict):
            continue
        existing = d.get("id")
        if existing is not None:
            try:
                next_id = max(next_id, int(existing) + 1)
            except (TypeError, ValueError):
                pass
            continue
        d["id"] = next_id
        next_id += 1
    return defects