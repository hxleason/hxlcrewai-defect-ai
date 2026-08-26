"""
app/core/regulation.py
法规检索模块 —— 内置结构化法规数据库（四级风险等级统一版）

功能：
    1. 根据“缺陷类型 + 风险等级”匹配法规引用、强制措施和检验建议；
    2. 支持别名映射，兼容“局部减薄”“未熔合”等非标准表述；
    3. 统一使用四级风险等级：低风险(1)、中风险(2)、高风险(3)、极高风险(4)；
    4. 匹配失败时自动降级，返回保守但可执行的工程建议；
    5. 无外部文件依赖，冷启动即可使用。

调用示例：
    from app.core.regulation import search_regulation

    result = search_regulation(defect_type="裂纹", risk_level=4)
    # {
    #     "law_references": "TSG 21-2016 第6.3.3条; GB/T 19624-2019",
    #     "mandatory_measures": "立即停机，全面检验，必要时更换受压元件。",
    #     "inspection_advice": "执行TOFD检测，补焊后热处理及耐压试验，并追溯同类设备。"
    # }

作者：动态FMEA智能体组
最后更新：2026-08-21
"""

import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("defect_fmea.regulation")

# ============================ 统一风险等级定义 ============================
# 全系统唯一风险等级映射，其他模块一律引用本定义，禁止再出现“可忽略/严重”等旧文字。

class RiskLevel:
    """风险等级数字与标准文字的统一映射"""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    EXTREME = 4

    TEXT_MAP = {
        LOW: "低风险",
        MEDIUM: "中风险",
        HIGH: "高风险",
        EXTREME: "极高风险",
    }


# ============================ 内置法规数据库 ============================
# 键格式: (缺陷类型, 风险等级)
# 值字段:
#   refs     : 法规/标准引用列表
#   measures : 强制处理措施
#   advice   : 检验或整改建议
# 风险等级: 1-4（对应低/中/高/极高）

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
        "refs": ["TSG 21-2016 第6.3.3条", "GB/T 19624-2019"],
        "measures": "立即停机，全面检验，必要时更换受压元件。",
        "advice": "执行TOFD，补焊后热处理及耐压试验。",
    },
    ("表面裂纹", 3): {
        "refs": ["TSG 21-2016 第6.3.2条", "GB/T 150.4-2011"],
        "measures": "打磨消除裂纹，补焊修复，焊后MT/UT检测。",
        "advice": "补焊前预热≥150℃，焊后消氢处理，增加检验频次。",
    },
    ("表面裂纹", 2): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "监控使用，缩短检验周期。",
        "advice": "表面PT检测，跟踪发展。",
    },
    ("表面裂纹", 1): {
        "refs": ["TSG 21-2016 第6.3.1条"],
        "measures": "记录在案，按正常周期检验。",
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
        "measures": "记录在案，按正常周期检验。",
        "advice": "定期宏观检查。",
    },
}

# ============================ 别名映射 ============================
# 前端/Agent 输出的类型名称 → 内置数据库中的标准名称。
# 优先级高于数据库键匹配，避免“微裂纹”无法命中“表面裂纹”细分条目。

TYPE_ALIAS: Dict[str, str] = {
    "局部减薄": "腐蚀",
    "腐蚀坑": "点蚀",
    "未熔合": "未焊透",
    "微裂纹": "表面裂纹",
    "内部裂纹": "裂纹",
    "均匀腐蚀": "腐蚀",
}


# ============================ 辅助解析函数 ============================

def _normalize_defect_type(defect_type: str) -> str:
    """
    将任意缺陷类型标准化为法规数据库中的标准名称。
    先查别名表，再按数据库键长度降序匹配。
    """
    if not defect_type:
        return ""

    # 1. 别名优先
    for alias, real in TYPE_ALIAS.items():
        if alias in defect_type and real:
            if any(k[0] == real for k in DEFAULT_REGULATION_DB):
                return real

    # 2. 数据库键长度降序匹配，防止“表面裂纹”被“裂纹”抢断
    candidates = sorted(DEFAULT_REGULATION_DB.keys(), key=lambda k: len(k[0]), reverse=True)
    for dtype, _ in candidates:
        if dtype in defect_type:
            return dtype

    return ""


def _parse_defect_type(query: str) -> str:
    """从查询字符串中提取缺陷类型"""
    return _normalize_defect_type(query)


def _parse_risk_level(query: str) -> int:
    """
    从查询字符串中提取统一四级风险等级。

    优先级：
        1. 极高风险 / 严重
        2. 高风险
        3. 中风险
        4. 低风险

    若无法识别则默认返回中风险(2)，保守处理。
    """
    q = query.lower()
    # 极高优先，避免被“高”抢断
    if "极高" in q or "严重" in q:
        return RiskLevel.EXTREME
    if "高风险" in q or "4级" in q or "等级4" in q or "level4" in q:
        return RiskLevel.EXTREME
    if "中风险" in q or "3级" in q or "等级3" in q or "level3" in q:
        return RiskLevel.HIGH
    if "低风险" in q or "2级" in q or "等级2" in q or "level2" in q:
        return RiskLevel.MEDIUM
    if "1级" in q or "等级1" in q or "level1" in q:
        return RiskLevel.LOW
    return RiskLevel.MEDIUM


def _match_regulation(defect_type: str, level: int) -> Dict[str, Any]:
    """
    在数据库中按 (缺陷类型, 风险等级) 精确匹配。
    若失败，从高等级向低等级逐级降级匹配，保证尽量返回有用建议。
    """
    key = (defect_type, level)
    if key in DEFAULT_REGULATION_DB:
        return DEFAULT_REGULATION_DB[key]

    # 降级匹配：从当前等级往下找
    for lv in range(level - 1, 0, -1):
        fallback_key = (defect_type, lv)
        if fallback_key in DEFAULT_REGULATION_DB:
            logger.info(f"未找到精确键 {key}，降级匹配 {fallback_key}")
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
    根据缺陷类型和风险等级返回法规信息。

    支持两种调用方式：
        1. search_regulation(defect_type="裂纹", risk_level=4)
        2. search_regulation("裂纹 高风险")

    Returns:
        dict: {
            "law_references":     "标准号1; 标准号2",
            "mandatory_measures": "强制处理措施",
            "inspection_advice":  "检验或整改建议"
        }
    """
    # 显式传参优先
    defect_type = kwargs.get("defect_type", "")
    level = kwargs.get("risk_level", 0)

    # 若显式参数缺失，从 query 中解析
    if not defect_type:
        defect_type = _parse_defect_type(query)
    else:
        # 对显式传入的类型也做一次别名/降级归一化
        defect_type = _normalize_defect_type(str(defect_type))

    if not level:
        level = _parse_risk_level(query)

    # 类型为空时采用保守默认
    if not defect_type:
        defect_type = "裂纹"  # 默认按最严重的判断路径返回，确保不会给出过低的建议
        logger.warning("无法从输入中解析缺陷类型，使用默认类型‘裂纹’进行法规检索")

    level = int(level)
    if level not in RiskLevel.TEXT_MAP:
        level = RiskLevel.MEDIUM
        logger.warning(f"风险等级 {level} 非法，已重置为中风险(2)")

    logger.info(
        "法规检索: 缺陷类型=%s, 等级=%s(%s)",
        defect_type, level, RiskLevel.TEXT_MAP[level]
    )

    matched = _match_regulation(defect_type, level)

    return {
        "law_references": "; ".join(matched["refs"]),
        "mandatory_measures": matched["measures"],
        "inspection_advice": matched["advice"],
    }


def load_regulation_folder(folder_path: str = None) -> int:
    """
    法规文件预加载函数（占位）。
    当前版本使用内置数据库，不依赖外部文件。
    """
    logger.info("法规加载模块: 当前使用内置结构化数据库，无需加载外部文件。")
    return 0