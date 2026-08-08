"""
法规检索模块 —— 基于内置结构化数据库
支持按缺陷类型 + 风险等级匹配法规引用、强制措施、检查建议
无需外部文件或向量库
"""

import logging
from typing import Dict, Any

logger = logging.getLogger("defect_fmea.regulation")

# ============================ 内置法规数据库 ============================
# 键格式: (缺陷类型, 风险等级)
# 值字段: refs (list of str), measures (str), advice (str)
# 风险等级: 1-4 (对应可忽略/低/中/高)

DEFAULT_REGULATION_DB: Dict[tuple, Dict[str, Any]] = {
    # ------------------ 裂纹类（通用）------------------
    ("裂纹", 4): {
        "refs": ["TSG 21-2016 第6.3.3条", "GB/T 19624-2019"],
        "measures": "立即停机，全面检验，必要时更换受压元件。",
        "advice": "执行TOFD检测，补焊后热处理及耐压试验，并追溯同类设备。",
    },
    ("裂纹", 3): {
        "refs": ["TSG 21-2016 第6.3.2条", "GB/T 150.4-2011"],
        "measures": "打磨消除裂纹，补焊修复，焊后MT/UT检测。",
        "advice": "补焊前预热≥150℃，焊后消氢处理，增加检验频次。",
    },
    ("裂纹", 2): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "监控使用，缩短检验周期。",
        "advice": "进行表面PT检测，测厚对比，跟踪发展。",
    },
    ("裂纹", 1): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "记录在案，按正常周期检验。",
        "advice": "定期测厚和宏观检查。",
    },

    # ------------------ 表面裂纹（独立细化）------------------
    ("表面裂纹", 4): {
        "refs": ["TSG 21-2016 第6.3.3条", "GB/T 19624"],
        "measures": "立即停机，全面检验，必要时更换受压元件。",
        "advice": "执行TOFD，补焊后热处理及耐压试验。",
    },
    ("表面裂纹", 3): {
        "refs": ["TSG 21-2016 第6.3.2条", "GB/T 150.4"],
        "measures": "打磨消除裂纹，补焊修复，焊后MT/UT检测。",
        "advice": "补焊前预热≥150℃，焊后消氢处理。",
    },
    ("表面裂纹", 2): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "监控使用，缩短检验周期。",
        "advice": "表面PT检测，跟踪发展。",
    },
    ("表面裂纹", 1): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "记录在案。",
        "advice": "定期宏观检查。",
    },

    # ------------------ 点蚀 / 腐蚀坑 ------------------
    ("点蚀", 4): {
        "refs": ["TSG 21-2016 第6.2.3条", "API 579-1/ASME FFS-1"],
        "measures": "堆焊修复或更换筒节，消除腐蚀源。",
        "advice": "修复后金相检验和耐压试验，加强介质监测。",
    },
    ("点蚀", 3): {
        "refs": ["TSG 21-2016 第6.2.2条"],
        "measures": "局部挖补修复，做好防腐处理。",
        "advice": "挖补后耐压试验，定期测厚。",
    },
    ("点蚀", 2): {
        "refs": ["TSG 21-2016 第6.2.1条"],
        "measures": "测厚监控，加密检查。",
        "advice": "定期壁厚测定，进行防腐层维护。",
    },
    ("点蚀", 1): {
        "refs": ["TSG 21-2016 第6.2.1条"],
        "measures": "记录腐蚀情况，按原计划检验。",
        "advice": "清洗表面，保持干燥。",
    },

    # ------------------ 腐蚀（通用）------------------
    ("腐蚀", 4): {
        "refs": ["TSG 21-2016 第6.2.3条"],
        "measures": "评估剩余强度，必要时更换。",
        "advice": "全面测厚，进行强度校核。",
    },
    ("腐蚀", 3): {
        "refs": ["TSG 21-2016 第6.2.2条"],
        "measures": "局部修复，加强防腐。",
        "advice": "修复后测厚验证。",
    },
    ("腐蚀", 2): {
        "refs": ["TSG 21-2016 第6.2.1条"],
        "measures": "监控壁厚变化。",
        "advice": "加密测厚，必要时防腐。",
    },
    ("腐蚀", 1): {
        "refs": ["TSG 21-2016 第6.2.1条"],
        "measures": "按正常周期检验。",
        "advice": "注意防腐层完好。",
    },

    # ------------------ 气孔 / 夹渣（焊接缺陷）------------------
    ("气孔", 4): {
        "refs": ["NB/T 47013-2015", "GB/T 150.4-2011"],
        "measures": "打磨消除，补焊并做RT/UT检测。",
        "advice": "加强焊材管理，改善焊接工艺。",
    },
    ("气孔", 3): {
        "refs": ["NB/T 47013-2015"],
        "measures": "打磨消除，补焊后PT检测。",
        "advice": "注意焊接参数，避免类似缺陷。",
    },
    ("气孔", 2): {
        "refs": ["NB/T 47013-2015"],
        "measures": "记录在案，下次检验跟踪。",
        "advice": "宏观检查，对比变化。",
    },
    ("气孔", 1): {
        "refs": ["NB/T 47013-2015"],
        "measures": "按正常程序验收。",
        "advice": "无需特殊处理。",
    },
    ("夹渣", 4): {
        "refs": ["NB/T 47013-2015", "GB/T 150.4-2011"],
        "measures": "打磨清除，补焊并RT检测。",
        "advice": "清理焊道，严格执行焊接规程。",
    },
    ("夹渣", 3): {
        "refs": ["NB/T 47013-2015"],
        "measures": "打磨清除，补焊后PT检测。",
        "advice": "改善焊前清理和层间清理。",
    },
    ("夹渣", 2): {
        "refs": ["NB/T 47013-2015"],
        "measures": "记录缺陷，下次检验确认。",
        "advice": "宏观跟踪。",
    },
    ("夹渣", 1): {
        "refs": ["NB/T 47013-2015"],
        "measures": "按正常程序验收。",
        "advice": "无需特殊处理。",
    },

    # ------------------ 未焊透（常见焊接缺陷）------------------
    ("未焊透", 4): {
        "refs": ["TSG 21-2016 第6.3.3条", "NB/T 47013-2015"],
        "measures": "立即停机，打磨补焊并RT/UT检测。",
        "advice": "补焊后热处理及耐压试验，追溯同批焊缝。",
    },
    ("未焊透", 3): {
        "refs": ["TSG 21-2016 第6.3.2条"],
        "measures": "打磨消除，补焊修复，焊后MT/UT检测。",
        "advice": "热处理后检测，增加检验频次。",
    },
    ("未焊透", 2): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "监控使用，缩短检验周期。",
        "advice": "表面PT检测，跟踪发展。",
    },
    ("未焊透", 1): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "记录在案。",
        "advice": "定期宏观检查。",
    },
}

# ============================ 别名映射 ============================
# 前端/Agent 输出的类型名称 → 内置数据库中的标准名称
TYPE_ALIAS = {
    "局部减薄": "腐蚀",
    "腐蚀坑": "点蚀",
    "未熔合": "未焊透",
    "微裂纹": "表面裂纹",
    "内部裂纹": "裂纹",
    "均匀腐蚀": "腐蚀",
    # 以下类型暂无对应，保持原样或 fallback
    # "机械磨损": None,
    # "变形": None,
    # "焊缝咬边": None,
}


# ============================ 辅助解析函数 ============================
def _parse_defect_type(query: str) -> str:
    """从查询字符串中提取缺陷类型（支持别名和模糊匹配）"""
    # 1. 优先匹配别名（别名可能不在数据库中，但指向的正式类型在库中）
    for alias, real in TYPE_ALIAS.items():
        if alias in query and real is not None:
            if any(k[0] == real for k in DEFAULT_REGULATION_DB):
                return real
    # 2. 按数据库键长度降序匹配，防止“表面裂纹”被“裂纹”抢断
    candidates = sorted(DEFAULT_REGULATION_DB.keys(), key=lambda k: len(k[0]), reverse=True)
    for (dtype, _) in candidates:
        if dtype in query:
            return dtype
    return ""


def _parse_risk_level(query: str) -> int:
    """从查询字符串中提取风险等级（1-4），若未指定则默认返回2"""
    if "高风险" in query or "4级" in query or "等级4" in query or "level4" in query.lower():
        return 4
    if "中风险" in query or "3级" in query or "等级3" in query or "level3" in query.lower():
        return 3
    if "低风险" in query or "2级" in query or "等级2" in query or "level2" in query.lower():
        return 2
    if "可忽略" in query or "1级" in query or "等级1" in query or "level1" in query.lower():
        return 1
    return 2  # 默认等级（低风险）


def _match_regulation(defect_type: str, level: int) -> Dict[str, Any]:
    """在数据库中精确匹配，若失败则降级匹配，直到无匹配返回默认"""
    # 精确匹配
    key = (defect_type, level)
    if key in DEFAULT_REGULATION_DB:
        return DEFAULT_REGULATION_DB[key]
    # 降级匹配：尝试更低等级
    for lv in range(level - 1, 0, -1):
        fallback_key = (defect_type, lv)
        if fallback_key in DEFAULT_REGULATION_DB:
            logger.info(f"未找到 {key}，降级匹配 {fallback_key}")
            return DEFAULT_REGULATION_DB[fallback_key]
    # 完全无匹配
    return {
        "refs": ["无明确法规依据"],
        "measures": "基于工程经验评估",
        "advice": "请咨询专业工程师",
    }


# ============================ 公开接口 ============================
def search_regulation(query: str = "", **kwargs) -> dict:
    """
    根据查询字符串返回法规信息。
    支持自动解析缺陷类型和风险等级，也可通过 kwargs 显式传入：
      - defect_type: str
      - risk_level: int

    Returns:
        dict: {
            "law_references": "标准号1; 标准号2",
            "mandatory_measures": "强制处理措施",
            "inspection_advice": "检查建议"
        }
    """
    # 如果调用方显式传参，优先使用（适配 Agent 可能的直接调用）
    defect_type = kwargs.get("defect_type", "")
    level = kwargs.get("risk_level", 0)

    if not defect_type:
        defect_type = _parse_defect_type(query)
    if not level:
        level = _parse_risk_level(query)

    logger.info(f"法规检索: 缺陷类型={defect_type}, 等级={level}, 原始查询='{query}'")

    matched = _match_regulation(defect_type, level)

    return {
        "law_references": "; ".join(matched["refs"]),
        "mandatory_measures": matched["measures"],
        "inspection_advice": matched["advice"],
    }


def load_regulation_folder(folder_path: str = None) -> int:
    """
    法规文件预加载函数（占位）
    当前版本使用内置数据库，不需要外部文件。
    若未来需要从 regulations/ 文件夹加载，可在此实现。
    """
    logger.info("法规加载模块: 当前使用内置结构化数据库，无需加载外部文件。")
    return 0