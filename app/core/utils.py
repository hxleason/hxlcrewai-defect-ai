"""
app/core/utils.py – FMEA 核心工具函数（纯规则引擎驱动 v2.0）

提供：
    - fmea_calculator: 基于规则引擎的缺陷风险评估（无默认壁厚回退）
    - diagnosis_reasons: 缺陷成因反查（扩展映射表）

设计原则：
    - 壁厚缺失时拒绝降级评估，强制返回“无法评定”并记录警告。
    - 长度/深度缺失时不强制归零，交由规则引擎根据内置策略处理
      （规则引擎会跳过依赖缺失数值的升级规则，防止错误低估风险）。
    - 所有输出字段完整，保证下游流水线不因缺失 key 而崩溃。
"""

import logging
from typing import Optional, List, Dict, Any

from app.core.rule_engine import rule_engine

logger = logging.getLogger("defect_fmea.utils")


def fmea_calculator(
    defect_type: str = "",
    length_mm: Optional[float] = None,
    depth_mm: Optional[float] = None,
    wall_thickness: Optional[float] = None,
    quantity: int = 1,
    **kwargs,
) -> Dict[str, Any]:
    """
    计算给定缺陷的 FMEA 风险值（S / O / D / RPN）。

    参数
    ----
    defect_type : str
        缺陷类型（如 "裂纹", "气孔", "夹杂" 等）。
    length_mm : float or None
        缺陷长度（mm），None 表示未测量。将原样传给规则引擎。
    depth_mm : float or None
        缺陷深度（mm），None 表示未测量。
    wall_thickness : float or None
        构件设计壁厚（mm）。**必须由上游提供有效值，否则评估终止并返回错误**。
    quantity : int
        同类缺陷数量（默认 1）。

    返回
    ----
    dict
        评估结果，即使失败也返回统一结构的字典（error 字段为 True 时表示无效）。
    """
    # ---- 1. 壁厚有效性检查（强制要求） ----
    if wall_thickness is None or wall_thickness <= 0:
        logger.warning(
            "FMEA 评估终止：缺少有效壁厚（wall_thickness=%s）。"
            "请确保报告已提供设计壁厚，或检查提取流程。",
            wall_thickness,
        )
        return {
            "error": True,
            "message": "缺少有效壁厚，无法计算风险等级。请提供设计壁厚(mm)后重试。",
            "severity": 0,
            "occurrence": 0,
            "detection": 0,
            "rpn": 0,
            "risk_level": "无法评定",
            "level": 0,
            "standard_ref": "",
            "triggered_rules": [],
        }

    # ---- 2. 构建标准缺陷字典（不强制填充长度/深度默认值） ----
    defect = {
        "type": defect_type,
        "length_mm": length_mm if length_mm is not None else None,
        "depth_mm": depth_mm if depth_mm is not None else None,
        "wall_thickness": float(wall_thickness),   # 已确保有效
        "quantity": int(quantity) if quantity >= 1 else 1,
    }

    # ---- 3. 调用规则引擎 ----
    logger.debug("调用规则引擎评估缺陷: %s", defect)
    try:
        result = rule_engine.evaluate(defect)
    except Exception as e:
        logger.error("规则引擎评估异常: %s", e, exc_info=True)
        # 发生严重错误时返回安全兜底
        return {
            "error": True,
            "message": f"规则引擎内部错误: {e}",
            "severity": 0,
            "occurrence": 0,
            "detection": 0,
            "rpn": 0,
            "risk_level": "系统错误",
            "level": 0,
            "standard_ref": "",
            "triggered_rules": [],
        }

    logger.info(
        "FMEA 评估完成: type=%s, RPN=%s, risk_level=%s",
        defect_type, result.get("rpn"), result.get("risk_level"),
    )
    return result


def diagnosis_reasons(defect_type: str = "", **kwargs) -> List[str]:
    """
    根据缺陷类型反查可能的成因列表。

    参数
    ----
    defect_type : str
        缺陷类型字符串（支持包含关系，如 "表面裂纹" 会匹配 "裂纹"）。

    返回
    ----
    List[str]
        可能的原因列表；若无匹配则返回 ["未知原因（建议人工分析）"]。
    """
    # ---- 扩展成因映射表（可按需从外部知识库加载） ----
    reason_mapping: Dict[str, List[str]] = {
        "裂纹": [
            "焊接残余应力",
            "材料淬硬倾向",
            "疲劳载荷",
            "氢致裂纹",
            "应力腐蚀开裂",
        ],
        "表面裂纹": [
            "焊接残余应力",
            "材料淬硬倾向",
            "疲劳载荷",
            "氢致裂纹",
            "机械划伤",
        ],
        "内部裂纹": [
            "铸造缺陷",
            "锻造折叠",
            "热处理不当",
            "氢致裂纹",
        ],
        "点蚀": [
            "介质腐蚀性（如含Cl⁻）",
            "保护涂层破损",
            "长期潮湿环境",
            "异种金属接触腐蚀",
        ],
        "腐蚀": [
            "介质腐蚀性",
            "保护层失效",
            "酸性气体环境",
            "微生物腐蚀",
        ],
        "气孔": [
            "焊接保护气体不足",
            "焊材受潮",
            "坡口清理不净",
            "凝固收缩",
        ],
        "夹杂": [
            "焊前清理不彻底",
            "焊渣未清除",
            "原材料夹杂物",
            "冶炼缺陷",
        ],
        "未熔合": [
            "焊接电流过小",
            "焊接速度过快",
            "坡口设计不当",
            "层间清理不良",
        ],
        "变形": [
            "焊接热输入过大",
            "拘束应力过大",
            "装配不当",
            "材料热膨胀系数高",
        ],
        "磨损": [
            "长期摩擦工况",
            "润滑失效",
            "磨粒侵入",
            "材料硬度不足",
        ],
    }

    # 按关键字长度降序匹配，避免短关键字提前命中
    for key in sorted(reason_mapping.keys(), key=len, reverse=True):
        if key in defect_type:
            logger.debug(f"缺陷类型 '{defect_type}' 匹配成因 '{key}' → {reason_mapping[key]}")
            return reason_mapping[key]

    logger.debug(f"未找到缺陷类型 '{defect_type}' 的已知成因，返回默认值")
    return ["未知原因（建议人工分析）"]


# ---------------------------------------------------------------------------
# 直接运行测试
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    print("=== 正常评估 ===")
    res = fmea_calculator(
        defect_type="裂纹",
        length_mm=5.0,
        depth_mm=1.2,
        wall_thickness=8.0,
        quantity=2,
    )
    print(res)

    print("\n=== 壁厚缺失评估（应返回错误） ===")
    res_err = fmea_calculator(
        defect_type="点蚀",
        length_mm=3.0,
        depth_mm=None,
        wall_thickness=None,
    )
    print(res_err)

    print("\n=== 成因测试 ===")
    print("表面裂纹:", diagnosis_reasons("表面裂纹"))
    print("未知类型:", diagnosis_reasons("神秘缺陷"))