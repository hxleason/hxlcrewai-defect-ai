"""
铸造缺陷原因诊断器 —— 纯规则引擎
根据缺陷类型、等级、尺寸、数量等，返回可能的原因列表
"""

# 规则库：结构为 {缺陷类型: {等级或通用: [原因列表]}}
# 可以根据工艺经验无限制追加
DIAGNOSIS_RULES = {
    "气孔": {
        "common": [
            "砂型或砂芯排气不良",
            "浇注系统设计不合理，卷入气体",
            "型砂含水量过高",
            "浇注温度过低，气体来不及逸出",
            "金属液本身含气量高（熔炼脱氧不良）"
        ],
        "3级": ["严重：可能伴随冷隔或浇不足，检查浇注速度"],
        "4级": ["严重：批量气孔，排查熔炼工艺及砂处理系统"]
    },
    "裂纹": {
        "common": [
            "铸件结构壁厚差过大，冷却不均匀",
            "开箱过早，落砂温度过高",
            "型芯退让性差，收缩受阻",
            "合金成分不当（如含硫量过高引起热脆）",
            "热处理升温过快或淬火应力"
        ],
        "3级": ["较宽裂纹：可能材料本身存在低熔点杂质"],
        "4级": ["贯穿性裂纹：极危险，立即复查模具及浇冒口设计"]
    },
    "缩松": {
        "common": [
            "冒口补缩不足或位置不当",
            "浇注温度过高，液体收缩大",
            "铸件热节处未放置冷铁",
            "合金凝固区间宽"
        ],
        "3级": ["密集缩松：提高冒口效率或改用保温冒口"],
        "4级": ["大面积缩松：重新设计浇冒口系统"]
    },
    "夹杂": {
        "common": [
            "浇注系统挡渣效果差",
            "炉料不干净，熔炼渣未扒清",
            "浇包衬耐火材料脱落",
            "型砂强度低，冲砂"
        ],
        "3级": ["大量非金属夹杂：检查过滤网是否完好"],
        "4级": ["硬质夹杂：可能导致加工崩刀，全检熔炼记录"]
    },
}

def diagnose_defect(defect_type: str, grade: str, diameter_mm: float = 0, quantity: int = 0):
    """
    根据缺陷类型和等级等，返回可能的原因列表
    参数:
        defect_type: 缺陷类型，如 '气孔', '裂纹' 等
        grade: 缺陷等级，如 '1级', '2级', '3级', '4级' 等
        diameter_mm: 缺陷直径(可选，用于更精细规则)
        quantity: 缺陷数量(可选)
    返回:
        list[str] : 可能原因的中文描述列表
    """
    type_rules = DIAGNOSIS_RULES.get(defect_type)
    if not type_rules:
        return [f"未知缺陷类型 '{defect_type}'，无法自动诊断，建议人工分析。"]

    reasons = []

    # 先添加通用原因（所有等级都适用）
    common_reasons = type_rules.get("common", [])
    reasons.extend(common_reasons)

    # 再根据等级追加特殊原因（规则字典中可能有 '3级'、'4级' 等键）
    level_key = grade  # 比如 '3级'
    if level_key in type_rules:
        reasons.extend(type_rules[level_key])

    # 动态规则（示例）
    if quantity >= 10:
        reasons.append("缺陷数量较多，建议检查批量生产的工艺稳定性。")
    if diameter_mm > 5.0:
        reasons.append("缺陷尺寸较大，可能为宏观工艺异常，优先排查关键工序。")

    if not reasons:
        reasons.append("未匹配到特定原因，请人工复核。")

    return reasons


if __name__ == "__main__":
    test_defect = "气孔"
    test_grade = "3级"
    res = diagnose_defect(test_defect, test_grade, diameter_mm=3.0, quantity=12)
    print(f"缺陷: {test_defect} {test_grade} → 可能原因:")
    for i, r in enumerate(res, 1):
        print(f"{i}. {r}")