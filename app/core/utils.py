"""
app/core/utils.py - FMEA 核心工具函数（环境变量驱动版）

提供：
    - fmea_calculator : 基于规则引擎的缺陷风险评估
    - diagnosis_reasons: 缺陷成因反查
所有可调参数均支持通过环境变量覆盖，无需任何额外配置文件。
"""

import os
import logging
from typing import Optional, List, Dict, Any

from app.core.rule_engine import rule_engine

logger = logging.getLogger("defect_fmea.utils")

# ---------------------------------------------------------------------------
# 全局可调参数（环境变量覆盖，模块加载时确定）
# ---------------------------------------------------------------------------
DEFAULT_WALL_THICKNESS: float = float(
    os.getenv("DEFAULT_WALL_THICKNESS", 2.0)
)  # 默认壁厚（mm），若报告未提供则使用此值

# ---------------------------------------------------------------------------
# 公共函数
# ---------------------------------------------------------------------------

def fmea_calculator(
    defect_type: str = "",
    length_mm: Optional[float] = 0.0,
    depth_mm: Optional[float] = 0.0,
    wall_thickness: Optional[float] = None,          # None 时自动使用 DEFAULT_WALL_THICKNESS
    quantity: int = 1,
    **kwargs,
) -> Dict[str, Any]:
    """
    计算给定缺陷的 FMEA 风险值（严重度 S / 发生度 O / 探测度 D / RPN）。

    参数
    ----
    defect_type : str
        缺陷类型（如 "裂纹", "气孔", "夹杂" 等）。
    length_mm : float, optional
        缺陷长度（mm），0 表示无或未测量。
    depth_mm : float, optional
        缺陷深度（mm），0 表示无或未测量。
    wall_thickness : float, optional
        构件设计壁厚（mm）。若为 None 或 <=0，
        且环境变量未提供有效默认值，则返回无法评定。
    quantity : int
        同类缺陷数量（默认 1）。

    返回
    ----
    dict
        包含 severity / occurrence / detection / rpn / risk_level /
        level / standard_ref / triggered_rules 等字段的评估结果。
        若输入无效，返回包含 "error" 键的字典。
    """
    # ---- 1. 输入规范化 ----
    if length_mm is None:
        length_mm = 0.0
    if depth_mm is None:
        depth_mm = 0.0

    # 若未显式提供壁厚，回退到环境变量默认值
    if wall_thickness is None:
        wall_thickness = DEFAULT_WALL_THICKNESS

    # 最终有效性检查
    if wall_thickness is None or wall_thickness <= 0:
        logger.warning(
            "无法进行 FMEA 评估：缺少有效壁厚参数。"
            "请通过环境变量 DEFAULT_WALL_THICKNESS 设置，或在报告中明确设计壁厚。"
        )
        return {
            "error": "缺少有效壁厚参数，请在报告中明确设计壁厚(mm)后重新评估。",
            "severity": 0,
            "occurrence": 0,
            "detection": 0,
            "rpn": 0,
            "risk_level": "无法评定",
            "level": 0,
            "standard_ref": "N/A",
            "triggered_rules": [],
        }

    # ---- 2. 构建缺陷特征字典 ----
    defect = {
        "type": defect_type,
        "length_mm": length_mm,
        "depth_mm": depth_mm,
        "wall_thickness": wall_thickness,
        "quantity": quantity,
    }

    # ---- 3. 调用规则引擎 ----
    logger.debug(f"调用规则引擎评估缺陷: {defect}")
    result = rule_engine.evaluate(defect)
    logger.info(f"FMEA 评估完成: RPN={result.get('rpn')}, 风险等级={result.get('risk_level')}")
    return result


def diagnosis_reasons(defect_type: str = "", **kwargs) -> List[str]:
    """
    根据缺陷类型反查可能的成因列表。

    参数
    ----
    defect_type : str
        缺陷类型字符串（支持模糊匹配，如 "表面裂纹" 会匹配 "裂纹"）。

    返回
    ----
    List[str]
        可能的原因列表，若无匹配则返回 ["未知原因"]。
    """
    # 成因映射表（可按需扩展或迁移至外部知识库）
    reason_mapping: Dict[str, List[str]] = {
        "裂纹": ["焊接残余应力", "材料淬硬倾向", "疲劳载荷", "氢致裂纹"],
        "表面裂纹": ["焊接残余应力", "材料淬硬倾向", "疲劳载荷", "氢致裂纹"],
        "点蚀": ["介质腐蚀性", "保护层破损", "长期潮湿环境"],
        "气孔": ["焊接保护不良", "焊材潮湿"],
        "夹杂": ["焊前清理不彻底", "焊渣未清除"],
        # 可按需补充更多缺陷类型
    }

    for key, reasons in reason_mapping.items():
        if key in defect_type:
            logger.debug(f"缺陷类型 '{defect_type}' 匹配成因键 '{key}' → {reasons}")
            return reasons

    logger.debug(f"未找到缺陷类型 '{defect_type}' 的已知成因，返回默认值")
    return ["未知原因"]


# ---------------------------------------------------------------------------
# 直接运行测试（可选）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("=== FMEA 工具测试 ===")
    test_result = fmea_calculator(
        defect_type="裂纹",
        length_mm=5.0,
        depth_mm=1.2,
        wall_thickness=8.0,
        quantity=2,
    )
    print(test_result)
    print("\n成因测试:", diagnosis_reasons("表面裂纹"))