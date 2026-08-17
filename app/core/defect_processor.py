"""
app/core/defect_processor.py – 缺陷处理工具（v5.1 终极版）

完全剥离 LLM 调用逻辑，所有提取/评估/审计函数均为纯 Python 实现，
可由 Celery 安全并行调用。

关键改进 (v5.1)：
- extract_defects 直接调用新版 crews.extract_defects（返回 Pydantic 对象），
  并自动将 DefectBase.defect_type 映射为 rule_engine 期望的 'type' 字段。
- evaluate_one_defect 已适配新的扁平缺陷结构（无 dimensions 子对象），
  直接从顶层字段取值，并安全处理缺失数据。
- audit_one_defect 同样使用 'type' 字段，风险等级未知时标记「需人工判定」。
- 所有函数均保持纯 Python 实现，无额外 LLM 调用。
"""

import logging
from typing import List, Dict, Any

from app.crews import create_analysis_crew            # 新版 crews.extract_defects 的别名
from app.core.utils import fmea_calculator, diagnosis_reasons
from app.core.regulation import search_regulation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 辅助：将 DefectBase 字段转换为规则引擎兼容的扁平字典
# ---------------------------------------------------------------------------
def _normalize_defect_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 crews 提取的原始缺陷字典转换为下游函数统一使用的格式：
      - 'defect_type' → 'type'（规则引擎期望的字段名）
      - 保留顶层 length / depth / wall_thickness / quantity 等字段
      - 若原始数据存在嵌套的 'dimensions' 对象（旧版兼容），则自动展平
    """
    defect = raw.copy()

    # 1. 字段重命名：defect_type -> type
    if "defect_type" in defect and "type" not in defect:
        defect["type"] = defect.pop("defect_type")
    # 若都没有，设默认值
    defect.setdefault("type", "未知缺陷")

    # 2. 兼容旧版 dimensions 嵌套结构（如果存在）
    dims = defect.get("dimensions")
    if isinstance(dims, dict):
        # 将 dimensions 中的字段提升到顶层，但顶层已有值时不覆盖
        for key in ("length", "depth", "unit"):
            if key in dims and key not in defect:
                defect[key] = dims[key]
        # 移除 dimensions 避免下游误解
        defect.pop("dimensions", None)

    # 3. 确保数值字段类型正确（缺失时置为 None 而非 0）
    for num_key in ("length", "depth", "wall_thickness"):
        val = defect.get(num_key)
        if val is not None:
            try:
                defect[num_key] = float(val)
            except (TypeError, ValueError):
                logger.warning("字段 %s 的值无法转换为浮点数: %s，将保留原值", num_key, val)
        else:
            defect[num_key] = None

    # quantity 应为整数
    try:
        defect["quantity"] = int(defect.get("quantity", 1))
    except (TypeError, ValueError):
        defect["quantity"] = 1

    return defect


# ---------------------------------------------------------------------------
# 公共接口
# ---------------------------------------------------------------------------
def extract_defects(input_text: str) -> List[Dict[str, Any]]:
    """
    从非结构化检验报告中提取缺陷列表（仅一次 LLM 调用）。

    Returns:
        List[dict] : 每个字典为已规范化的缺陷数据，包含 'type' 字段等。
    Raises:
        ParsingError: 当 LLM 输出无法通过 schema 校验时。
        LLMTimeoutError / LLMAPIError: 当 LLM 接口调用失败时。
    """
    # 调用新版 extract_defects（已在 crews 中通过 output_pydantic 强制校验）
    pydantic_result = create_analysis_crew(input_text)       # -> DefectExtractionResult
    raw_defects = pydantic_result.defects                    # List[DefectBase]

    if not raw_defects:
        logger.info("提取结果：未发现任何缺陷。")
        return []

    # 转为字典并规范字段
    normalized = []
    for item in raw_defects:
        d = _normalize_defect_dict(item.model_dump())
        normalized.append(d)

    logger.info("成功提取 %d 条缺陷记录。", len(normalized))
    return normalized


def evaluate_one_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
    """
    对单条缺陷执行 FMEA 评级 + 失效原因诊断（纯规则引擎）。

    期望的输入缺陷字典已经过 _normalize_defect_dict 处理，包含 'type'、'length'、
    'depth'、'wall_thickness' 等字段。若字段缺失，规则引擎将采用保守策略。

    Returns:
        dict : 原始缺陷信息 + fmea 评分 (severity, occurrence, detection, rpn,
               risk_level, level, triggered_rules) + reasons
    """
    # 确保基础字段
    defect = _normalize_defect_dict(defect)   # 二次保险（幂等）

    dtype = defect.get("type", "")
    length = defect.get("length")            # 可能为 None
    depth = defect.get("depth")              # 可能为 None
    wall_thickness = defect.get("wall_thickness")
    quantity = defect.get("quantity", 1)

    logger.debug("评估缺陷: type=%s, length=%s, depth=%s, wall=%s, qty=%s",
                 dtype, length, depth, wall_thickness, quantity)

    # 调用核心规则引擎（已处理壁厚缺失 → 返回错误信息）
    fmea_result = fmea_calculator(
        defect_type=dtype,
        length_mm=length,
        depth_mm=depth,
        wall_thickness=wall_thickness,
        quantity=quantity,
    )

    # 失效原因诊断
    reasons = diagnosis_reasons(dtype)

    # 合并结果（fmea_result 中的字段覆盖原始字段，例如 severity 等）
    combined = {**defect, **fmea_result, "reasons": reasons}
    logger.info("缺陷评估完成: id=%s, RPN=%s, risk_level=%s",
                defect.get("id"), fmea_result.get("rpn"), fmea_result.get("risk_level"))
    return combined


def audit_one_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
    """
    为单条缺陷检索适用的法规条文和检验建议（基于结构化参数）。

    优先使用 numerical level（1-4），若不存在则根据中文风险等级字符串推断。
    当风险等级无法确定时，标记为“需人工判定”，避免错误调用法规。

    Returns:
        dict : 原始缺陷字典 + law_references / mandatory_measures / inspection_advice
    """
    defect = _normalize_defect_dict(defect)   # 确保字段一致

    dtype = defect.get("type", "")

    # 1. 确定数值风险等级
    level_num = defect.get("level")            # rule_engine 输出的数值等级
    if level_num is None:
        risk_str = defect.get("risk_level", "")
        # 简易映射 (与 rule_engine.RPN_LEVELS 对应：可忽略=1,低=2,中=3,高=4)
        if "高" in risk_str or "严重" in risk_str:
            level_num = 4
        elif "中" in risk_str:
            level_num = 3
        elif "低" in risk_str:
            level_num = 2
        elif "可忽略" in risk_str:
            level_num = 1
        else:
            level_num = 0   # 无法识别

    # 2. 结构化调用法规检索
    if level_num <= 0 or level_num > 4:
        law_info = {
            "law_references": "风险等级未知，无法自动匹配法规",
            "mandatory_measures": "需人工根据实际风险等级查阅相应标准",
            "inspection_advice": "请确认缺陷类型与严重程度后重新检索",
        }
        logger.warning("缺陷 %s 风险等级未知（level=%s），无法自动关联法规。",
                       defect.get("id"), level_num)
    else:
        law_info = search_regulation(defect_type=dtype, risk_level=level_num)

    # 3. 将法规信息并入缺陷记录（使用不可变副本以避免副作用）
    result = defect.copy()
    result["law_references"] = law_info.get("law_references", "")
    result["mandatory_measures"] = law_info.get("mandatory_measures", "")
    result["inspection_advice"] = law_info.get("inspection_advice", "")
    return result