"""
缺陷处理工具函数（v2 流水线专用）
剥离 LLM 调用，纯 Python 实现，可被 Celery 安全并行调用。
"""
import json, logging
from typing import List, Dict, Any

# **** 请根据您的项目实际路径修改以下导入 ****
from app.core.utils import fmea_calculator, diagnosis_reasons   # 规则引擎
from app.core.regulation import search_regulation               # 法规检索

logger = logging.getLogger(__name__)


def extract_defects(input_text: str) -> List[Dict[str, Any]]:
    """LLM 提取缺陷（只调用一次）"""
    from app.crew import create_analysis_crew

    raw = create_analysis_crew(input_text)
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip().startswith('{') else {"defects": []}
        except:
            data = {"defects": []}
    elif isinstance(raw, dict):
        data = raw
    else:
        data = {"defects": []}

    defects = data.get("defects", [])
    if not isinstance(defects, list):
        logger.warning("提取结果异常: %s", type(defects))
        return []
    return defects


def evaluate_one_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
    """对单条缺陷进行 FMEA 评级 + 原因诊断（规则引擎）"""
    dtype = defect.get("type", "")
    dims = defect.get("dimensions") or {}
    length = dims.get("length")
    depth = dims.get("depth")
    wall_thickness = defect.get("wall_thickness")
    quantity = defect.get("quantity", 1)

    fmea_result = fmea_calculator(
        defect_type=dtype,
        length_mm=length,
        depth_mm=depth,
        wall_thickness=wall_thickness,
        quantity=quantity,
    )
    reasons = diagnosis_reasons(dtype)
    return {**defect, **fmea_result, "reasons": reasons}


def audit_one_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
    """对单条缺陷检索法规条文"""
    dtype = defect.get("type", "")
    risk_level = defect.get("risk_level", 2)
    law_info = search_regulation(f"{dtype} 等级{risk_level} 处理措施")

    defect["law_references"] = law_info.get("law_references", "")
    defect["mandatory_measures"] = law_info.get("mandatory_measures", "")
    defect["inspection_advice"] = law_info.get("inspection_advice", "")
    return defect