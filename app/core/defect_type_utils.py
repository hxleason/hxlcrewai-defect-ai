"""
app/core/defect_type_utils.py
缺陷类型归一化公共工具模块

用途：
    统一维护缺陷类型别名映射和归一化函数，供 regulation.py 与 defect_processor.py 共同调用，
    避免两处独立映射导致的分叉（例如“未熔合”误映射为“未焊透”）。

本模块维护内容：
    1. STANDARD_DEFECT_TYPES：标准缺陷类型列表（与法规数据库键保持一致）
    2. TYPE_ALIAS：别名 → 标准类型映射（不包含任何错误映射）
    3. normalize_defect_type：将任意缺陷类型字符串归一化为标准类型或空字符串
"""

import logging

logger = logging.getLogger("defect_fmea.defect_utils")

# ============================ 标准缺陷类型列表 ============================
# 必须与 DEFAULT_REGULATION_DB 中的键完全一致（仅取缺陷类型部分，不含风险等级）
STANDARD_DEFECT_TYPES = [
    "裂纹",
    "表面裂纹",
    "点蚀",
    "腐蚀",
    "气孔",
    "夹渣",
    "未焊透",
]

# ============================ 别名映射 ============================
# 前端/Agent 输出的类型名称 → 标准类型名称
# 注意：
#   1. 不包含 "未熔合": "未焊透"（二者是不同的焊接缺陷，法规引用不同）
#   2. 若数据库未来增加新标准类型，需同步更新 STANDARD_DEFECT_TYPES
TYPE_ALIAS = {
    "局部减薄": "腐蚀",
    "腐蚀坑": "点蚀",
    "微裂纹": "表面裂纹",
    "内部裂纹": "裂纹",
    "均匀腐蚀": "腐蚀",
    # 焊接缺陷别名
    "表面裂缝": "表面裂纹",
    "纵向裂纹": "裂纹",
    "环向裂纹": "裂纹",
    "横向裂纹": "裂纹",
    "龟裂": "裂纹",
    "腐蚀减薄": "腐蚀",
    "局部腐蚀": "腐蚀",
    "点腐蚀": "点蚀",
    "点状腐蚀": "点蚀",
    "密集气孔": "气孔",
    "单个气孔": "气孔",
    "表面气孔": "气孔",
    "非金属夹杂": "夹渣",
    "夹杂": "夹渣",  # 将“夹杂”统一映射到数据库键“夹渣”
    "未熔合": "未熔合",  # 保持原样，不映射到未焊透
}


def normalize_defect_type(defect_type: str) -> str:
    """
    将任意缺陷类型字符串归一化为标准类型名称。

    归一化策略：
        1. 别名优先：若输入字符串中包含某个别名，直接返回映射后的标准类型；
        2. 标准类型降序匹配：按长度从长到短匹配标准类型，避免“表面裂纹”被“裂纹”截胡；
        3. 无法归一化时返回空字符串，由调用方决定降级逻辑。

    Args:
        defect_type: 原始缺陷类型字符串，可能为空或包含额外描述

    Returns:
        标准缺陷类型名称，若无法识别则返回空字符串。
    """
    if not defect_type:
        return ""

    # 1. 别名优先（仅当真实类型确实存在于标准列表时才生效）
    for alias, real in TYPE_ALIAS.items():
        if alias in defect_type:
            if real not in STANDARD_DEFECT_TYPES:
                logger.warning(
                    "别名 %s 映射到不在标准列表中的类型 %s，已忽略该别名",
                    alias, real,
                )
                continue
            return real

    # 2. 标准类型降序匹配（优先匹配更具体的类型）
    sorted_standards = sorted(STANDARD_DEFECT_TYPES, key=len, reverse=True)
    for std in sorted_standards:
        if std in defect_type:
            return std

    return ""