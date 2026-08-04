from typing import Optional, Dict, Any, List
from app.config import DEFAULT_WALL_THICKNESS
from app.core.rule_engine import rule_engine

def fmea_calculator(
    defect_type: str = "",
    length_mm: Optional[float] = 0.0,
    depth_mm: Optional[float] = 0.0,
    wall_thickness: Optional[float] = DEFAULT_WALL_THICKNESS,
    quantity: int = 1,
    **kwargs
) -> dict:
    if length_mm is None: length_mm = 0.0
    if depth_mm is None: depth_mm = 0.0

    if wall_thickness is None or wall_thickness <= 0:
        return {
            "error": "缺少有效壁厚参数，请在报告中明确设计壁厚(mm)后重新评估。",
            "severity": 0,
            "occurrence": 0,
            "detection": 0,
            "rpn": 0,
            "risk_level": "无法评定",
            "level": 0,
            "standard_ref": "N/A",
            "triggered_rules": []
        }

    defect = {
        "type": defect_type,
        "length_mm": length_mm,
        "depth_mm": depth_mm,
        "wall_thickness": wall_thickness,
        "quantity": quantity,
    }
    return rule_engine.evaluate(defect)

def diagnosis_reasons(defect_type: str = "", **kwargs) -> List[str]:
    mapping = {
        "裂纹": ["焊接残余应力", "材料淬硬倾向", "疲劳载荷", "氢致裂纹"],
        "表面裂纹": ["焊接残余应力", "材料淬硬倾向", "疲劳载荷", "氢致裂纹"],
        "点蚀": ["介质腐蚀性", "保护层破损", "长期潮湿环境"],
        "气孔": ["焊接保护不良", "焊材潮湿"],
        "夹杂": ["焊前清理不彻底", "焊渣未清除"],
    }
    for key in mapping:
        if key in defect_type:
            return mapping[key]
    return ["未知原因"]