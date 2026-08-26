"""
app/core/utils.py – FMEA 核心工具函数（知识库增强版 v3.2）

提供：
    - fmea_calculator: 基于规则引擎 + 专家规则 + 案例库的缺陷风险评估
    - diagnosis_reasons: 缺陷成因反查（原始映射 + 案例根因提取）

设计原则：
    - 壁厚缺失时**不再直接报错**，而是尝试从案例库获取基准 S/O/D（降级评估）。
      若案例库也无匹配，则返回错误（保留原始行为）。
    - 专家规则根据缺陷特征动态调整 S/O/D，并记录规则应用情况。
    - 相似案例检索作为附加信息返回，供下游进一步分析或人工审核。
    - 所有输出字段完整，保证下游流水线不因缺失 key 而崩溃。

v3.2 修复与增强：
    - 壁厚字段名兼容：支持 wall_thickness / design_wall_thickness / nominal_thickness 等。
    - 修复案例库数据存在 Ellipsis 等异常类型时导致 .lower() 报错的问题。
    - 增加对案例库返回值的类型检查，确保可靠性。
    - 相似案例检索增加更多字段容错。
"""

import logging
from typing import Optional, List, Dict, Any, Tuple

# 导入基础规则引擎（保留）
from app.core.rule_engine import rule_engine
# 导入知识库引擎（新增）
from app.core.knowledge_base import KnowledgeBase, get_knowledge_base

logger = logging.getLogger("defect_fmea.utils")

# ---------------------------------------------------------------------------
# 知识库惰性加载（避免模块导入时因文件缺失导致崩溃）
# ---------------------------------------------------------------------------
_kb_instance = None  # 存储单例实例，若加载失败则标记为 None
_kb_attempted = False


def _get_kb() -> Optional[KnowledgeBase]:
    """
    获取知识库实例（惰性加载，多次调用安全）。
    如果加载失败（文件缺失、格式错误等），返回 None 并记录错误。
    """
    global _kb_instance, _kb_attempted
    if not _kb_attempted:
        _kb_attempted = True
        try:
            _kb_instance = get_knowledge_base()
        except Exception as e:
            logger.error("知识库加载失败，专家规则和案例检索将被禁用: %s", e)
            _kb_instance = None  # 明确设置为 None
    return _kb_instance


def _calculate_risk_level(rpn: int) -> Tuple[str, int]:
    """
    根据 RPN 值计算风险等级文字描述和等级数值（全系统统一四级）。

    等级划分：
        RPN ≤ 50      -> 低风险   (level 1)
        50 < RPN ≤ 100 -> 中风险  (level 2)
        100 < RPN ≤ 200 -> 高风险 (level 3)
        RPN > 200      -> 极高风险 (level 4)
    """
    if rpn > 200:
        return "极高风险", 4
    elif rpn > 100:
        return "高风险", 3
    elif rpn > 50:
        return "中风险", 2
    else:
        return "低风险", 1


def _extract_wall_thickness_from_kwargs(kwargs: Dict[str, Any]) -> Optional[float]:
    """
    从 **kwargs 中提取壁厚值，支持多种常见字段名。
    返回 None 表示未找到有效壁厚。
    """
    # 按优先级检查字段
    candidates = ["wall_thickness", "design_wall_thickness", "nominal_thickness", "thickness"]
    for key in candidates:
        val = kwargs.get(key)
        if val is not None:
            try:
                val_float = float(val)
                if val_float > 0:
                    return val_float
            except (TypeError, ValueError):
                continue
    return None


def fmea_calculator(
    defect_type: str = "",
    length_mm: Optional[float] = None,
    depth_mm: Optional[float] = None,
    wall_thickness: Optional[float] = None,
    quantity: int = 1,
    media: str = "",
    material: str = "",
    device_type: str = "",
    environment: str = "",
    operating_temperature: Optional[float] = None,
    design_pressure: Optional[float] = None,
    location: str = "",
    **kwargs,
) -> Dict[str, Any]:
    """
    计算给定缺陷的 FMEA 风险值（S / O / D / RPN），并整合专家规则和案例库信息。

    参数（同 v3.1 但增加壁厚字段兼容）：
    ----------
    defect_type : str
        缺陷类型（如 "裂纹", "点蚀" 等）。
    length_mm : float or None
        缺陷长度（mm），None 表示未测量。
    depth_mm : float or None
        缺陷深度（mm），None 表示未测量。
    wall_thickness : float or None
        构件设计壁厚（mm）。如果为 None，会尝试从 **kwargs 中寻找备用壁厚字段。
    quantity : int
        同类缺陷数量（默认 1）。
    media, material, device_type, environment, operating_temperature, design_pressure, location
        其他特征，用于专家规则匹配和案例检索。
    **kwargs
        额外参数，可能包含壁厚的备用字段或 features 字典。

    返回
    ----
    dict
        评估结果，包含以下字段：
        - error : bool
        - message : str
        - warning : Optional[str]
        - review_required : bool
        - severity / occurrence / detection : int
        - rpn : int
        - risk_level : str
        - level : int
        - standard_ref : str
        - triggered_rules : List
        - rule_applications : List
        - similar_cases : List
        - similar_case_ids : List
        - similar_case_measures : List
        - base_source : str
        - source_case : Optional[str]
    """
    logger.debug("FMEA 评估输入: %s", {
        "defect_type": defect_type, "length_mm": length_mm, "depth_mm": depth_mm,
        "wall_thickness": wall_thickness, "quantity": quantity, "media": media,
        "material": material, "device_type": device_type, "environment": environment,
        "extra_kwargs": {k: v for k, v in kwargs.items() if k != "features"}
    })

    # ------------------------------------------------------------------
    # 0. 壁厚兼容性处理：若 wall_thickness 未提供或无效，尝试从 kwargs 中提取
    # ------------------------------------------------------------------
    if wall_thickness is None or wall_thickness <= 0:
        # 1) 检查 kwargs 是否直接包含备用壁厚字段
        backup_thickness = _extract_wall_thickness_from_kwargs(kwargs)
        if backup_thickness is not None:
            logger.info("从 **kwargs 中解析到备用壁厚: %s", backup_thickness)
            wall_thickness = backup_thickness
        else:
            # 2) 检查 kwargs 是否包含 'features' 字典（内部含壁厚）
            features_arg = kwargs.get("features")
            if isinstance(features_arg, dict):
                for key in ["wall_thickness", "design_wall_thickness", "nominal_thickness", "thickness"]:
                    val = features_arg.get(key)
                    if val is not None:
                        try:
                            val_float = float(val)
                            if val_float > 0:
                                logger.info("从 features 字典中解析到壁厚: %s", val_float)
                                wall_thickness = val_float
                                break
                        except (TypeError, ValueError):
                            continue

    # ------------------------------------------------------------------
    # 1. 获取基础 S/O/D 和相关信息
    # ------------------------------------------------------------------
    base_s = base_o = base_d = None
    base_source = ""
    source_case = None
    standard_ref = ""
    triggered_rules = []
    warning = None
    review_required = False

    # 构建用于知识库匹配的特征字典
    features = {
        "defect_type": defect_type,
        "media": media,
        "material": material,
        "device_type": device_type,
        "environment": environment,
        "operating_temperature": operating_temperature,
        "design_pressure": design_pressure,
        "location": location,
        "length_mm": length_mm,
        "depth_mm": depth_mm,
        "wall_thickness": wall_thickness,
    }

    # 壁厚有效时的标准路径：调用基础规则引擎
    if wall_thickness is not None and wall_thickness > 0:
        defect = {
            "type": defect_type,
            "length_mm": length_mm if length_mm is not None else None,
            "depth_mm": depth_mm if depth_mm is not None else None,
            "wall_thickness": float(wall_thickness),
            "quantity": int(quantity) if quantity >= 1 else 1,
        }
        logger.debug("调用基础规则引擎评估缺陷: %s", defect)
        try:
            base_result = rule_engine.evaluate(defect)
        except Exception as e:
            logger.error("基础规则引擎评估异常: %s", e, exc_info=True)
            return {
                "error": True,
                "message": f"规则引擎内部错误: {e}",
                "warning": None,
                "review_required": False,
                "severity": 0,
                "occurrence": 0,
                "detection": 0,
                "rpn": 0,
                "risk_level": "系统错误",
                "level": 0,
                "standard_ref": "",
                "triggered_rules": [],
                "rule_applications": [],
                "similar_cases": [],
                "similar_case_ids": [],
                "similar_case_measures": [],
                "base_source": "",
                "source_case": None,
            }

        # 从基础结果中提取数据
        base_s = base_result.get("severity", 0)
        base_o = base_result.get("occurrence", 0)
        base_d = base_result.get("detection", 0)
        standard_ref = base_result.get("standard_ref", "")
        triggered_rules = base_result.get("triggered_rules", [])
        base_source = "rule_engine"

    else:
        # ------------------------------------------------------------------
        # 壁厚缺失时的降级处理：尝试从案例库获取基准 S/O/D
        # ------------------------------------------------------------------
        logger.warning("壁厚缺失，尝试从案例库获取基准 S/O/D")
        warning = "缺少有效壁厚，已基于历史案例库的相似案例基准 S/O/D 进行评估，建议人工复核。"
        review_required = True

        kb = _get_kb()
        if kb is None:
            # 知识库不可用，无法降级，返回错误
            return {
                "error": True,
                "message": "缺少有效壁厚且知识库不可用，无法计算风险等级。请提供设计壁厚(mm)后重试。",
                "warning": warning,
                "review_required": True,
                "severity": 0,
                "occurrence": 0,
                "detection": 0,
                "rpn": 0,
                "risk_level": "无法评定",
                "level": 0,
                "standard_ref": "",
                "triggered_rules": [],
                "rule_applications": [],
                "similar_cases": [],
                "similar_case_ids": [],
                "similar_case_measures": [],
                "base_source": "case_baseline",
                "source_case": None,
            }

        try:
            # 使用设备类型和缺陷类型进行匹配
            avg_s, avg_o, avg_d = kb.get_case_baseline_sod(
                device_class=device_type,
                failure_mode=defect_type,
            )
            # 防御性检查：确保获取到的是数值类型，且不能为 None 或非正数
            avg_s = float(avg_s) if isinstance(avg_s, (int, float)) and avg_s > 0 else None
            avg_o = float(avg_o) if isinstance(avg_o, (int, float)) and avg_o > 0 else None
            avg_d = float(avg_d) if isinstance(avg_d, (int, float)) and avg_d > 0 else None
            if avg_s is None or avg_o is None or avg_d is None:
                raise ValueError("案例库返回的 S/O/D 无效")
        except Exception as e:
            logger.error("从案例库获取基准 S/O/D 失败: %s", e)
            avg_s = avg_o = avg_d = None

        if avg_s is None or avg_o is None or avg_d is None:
            return {
                "error": True,
                "message": "壁厚缺失且未在案例库中找到匹配案例，无法给出可靠评估。请提供设计壁厚(mm)或人工分析。",
                "warning": warning,
                "review_required": True,
                "severity": 0,
                "occurrence": 0,
                "detection": 0,
                "rpn": 0,
                "risk_level": "无法评定",
                "level": 0,
                "standard_ref": "",
                "triggered_rules": [],
                "rule_applications": [],
                "similar_cases": [],
                "similar_case_ids": [],
                "similar_case_measures": [],
                "base_source": "case_baseline",
                "source_case": None,
            }

        base_s, base_o, base_d = int(avg_s), int(avg_o), int(avg_d)
        base_source = "case_baseline"
        source_case = "相似案例平均基准"  # 可进一步细化
        standard_ref = "基于案例库统计平均值"

    # ------------------------------------------------------------------
    # 2. 知识库增强：专家规则匹配与调整
    # ------------------------------------------------------------------
    rule_applications = []
    adjusted_s, adjusted_o, adjusted_d = base_s, base_o, base_d

    kb = _get_kb()
    if kb is not None:
        try:
            matched_rules = kb.match_expert_rules(features)
            if matched_rules:
                adjusted_s, adjusted_o, adjusted_d, rule_applications = kb.apply_rule_adjustments(
                    base_s, base_o, base_d, matched_rules
                )
                # 将专家规则 ID 加入 triggered_rules
                for app in rule_applications:
                    triggered_rules.append({
                        "rule_id": app["rule_id"],
                        "rule_class": app["rule_class"],
                        "source": "expert_rule",
                    })
        except Exception as e:
            logger.error("专家规则匹配/调整失败: %s", e, exc_info=True)
            # 失败不影响基础结果，继续使用基础值
            adjusted_s, adjusted_o, adjusted_d = base_s, base_o, base_d
    else:
        logger.debug("知识库不可用，跳过专家规则调整")

    # ------------------------------------------------------------------
    # 3. 相似案例检索
    # ------------------------------------------------------------------
    similar_cases = []
    similar_case_ids = []
    similar_case_measures = []

    if kb is not None:
        try:
            raw_cases = kb.search_similar_cases(features, top_k=5)
            # 确保 raw_cases 是列表
            if not isinstance(raw_cases, list):
                raw_cases = []
            for item in raw_cases:
                # 防御性提取，确保 item 是字典且包含必要字段
                if not isinstance(item, dict):
                    continue
                case_id = item.get("case_id", "未知案例")
                case_detail = item.get("case", {})
                similarity = item.get("similarity", 0.0)
                measures = item.get("measures", [])
                if not isinstance(measures, list):
                    measures = []
                similar_cases.append({
                    "case_id": case_id,
                    "similarity": similarity,
                    "case_detail": case_detail,
                    "measures": measures,
                })
                similar_case_ids.append(case_id)
                # 收集措施（仅字符串）
                for measure in measures:
                    if isinstance(measure, str) and measure.strip():
                        similar_case_measures.append(measure.strip())
            # 去重措施
            similar_case_measures = list(dict.fromkeys(similar_case_measures))
        except Exception as e:
            logger.error("相似案例检索失败: %s", e, exc_info=True)
    else:
        logger.debug("知识库不可用，跳过相似案例检索")

    # ------------------------------------------------------------------
    # 4. 计算最终 RPN 和风险等级
    # ------------------------------------------------------------------
    rpn = adjusted_s * adjusted_o * adjusted_d
    risk_level, level = _calculate_risk_level(rpn)

    result = {
        "error": False,
        "message": "",
        "warning": warning,
        "review_required": review_required,
        "severity": adjusted_s,
        "occurrence": adjusted_o,
        "detection": adjusted_d,
        "rpn": rpn,
        "risk_level": risk_level,
        "level": level,
        "standard_ref": standard_ref,
        "triggered_rules": triggered_rules,
        "rule_applications": rule_applications,
        "similar_cases": similar_cases,
        "similar_case_ids": similar_case_ids,
        "similar_case_measures": similar_case_measures,
        "base_source": base_source,
        "source_case": source_case,
    }

    logger.info(
        "FMEA 评估完成: type=%s, 基础SOD=%s/%s/%s, 调整后SOD=%s/%s/%s, RPN=%s, 风险=%s",
        defect_type,
        base_s, base_o, base_d,
        adjusted_s, adjusted_o, adjusted_d,
        rpn, risk_level,
    )
    return result


# ---------------------------------------------------------------------------
# 成因反查函数（增强版：包含案例根因提取）
# ---------------------------------------------------------------------------
def diagnosis_reasons(defect_type: str = "", **kwargs) -> List[str]:
    """
    根据缺陷类型反查可能的成因列表，并尝试从知识库的相似案例中提取根因补充。

    参数
    ----
    defect_type : str
        缺陷类型字符串（支持包含关系，如 "表面裂纹" 会匹配 "裂纹"）。
    **kwargs : 可选的额外特征（如 media, material 等），用于案例检索。

    返回
    ----
    List[str]
        可能的原因列表；若无匹配则返回 ["未知原因（建议人工分析）"]。
    """
    # ---- 原有成因映射表（保持不变） ----
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
    reasons: List[str] = []
    for key in sorted(reason_mapping.keys(), key=len, reverse=True):
        if key in defect_type:
            reasons.extend(reason_mapping[key])
            break

    # ------------------------------------------------------------------
    # 尝试从知识库相似案例中提取根因（补充）
    # ------------------------------------------------------------------
    kb = _get_kb()
    if kb is not None and defect_type:
        # 构建特征用于案例检索（仅使用已有的非空 kwargs）
        search_features = {"defect_type": defect_type}
        # 从 kwargs 中提取有意义的字段
        for field in ["media", "material", "device_type", "environment", "location"]:
            if field in kwargs and kwargs[field]:
                search_features[field] = kwargs[field]

        try:
            similar = kb.search_similar_cases(search_features, top_k=3)
            if isinstance(similar, list):
                for item in similar:
                    if not isinstance(item, dict):
                        continue
                    case = item.get("case", item)  # 兼容不同返回结构
                    # 提取案例中可能的根因字段（根据实际案例库字段名调整）
                    root_cause_fields = ["root_cause", "failure_cause", "cause", "reason"]
                    for field in root_cause_fields:
                        if field in case and isinstance(case[field], str):
                            cause_text = case[field].strip()
                            if cause_text and cause_text not in reasons:
                                reasons.append(cause_text)
        except Exception as e:
            logger.debug("案例根因提取失败: %s", e)

    if not reasons:
        return ["未知原因（建议人工分析）"]
    return reasons


# ---------------------------------------------------------------------------
# 直接运行测试
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    print("=== 正常评估（壁厚有效） ===")
    res = fmea_calculator(
        defect_type="裂纹",
        length_mm=5.0,
        depth_mm=1.2,
        wall_thickness=8.0,
        quantity=2,
        media="液氨",
        material="Q345R",
        device_type="移动式压力容器",
    )
    print(res)

    print("\n=== 壁厚缺失评估（应使用案例库降级） ===")
    res_nothick = fmea_calculator(
        defect_type="点蚀",
        length_mm=3.0,
        depth_mm=None,
        wall_thickness=None,
        media="液氨",
        material="Q345R",
        device_type="移动式压力容器",
    )
    print(res_nothick)

    print("\n=== 成因测试 ===")
    print("表面裂纹:", diagnosis_reasons("表面裂纹"))
    print("点蚀 + 介质液氨:", diagnosis_reasons("点蚀", media="液氨"))
    print("未知类型:", diagnosis_reasons("神秘缺陷"))