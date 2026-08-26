"""
app/core/defect_processor.py – 缺陷处理工具（v7.4 集成版 · LLM 上下文自动生成）

完全剥离 LLM 调用逻辑，所有提取/评估/审计函数均为纯 Python 实现，
可由 Celery 安全并行调用。
v7.4 新增：
- 在 audit_one_defect 结果中自动生成 llm_context 字段，
  整合缺陷全量信息（含相似案例措施），供上层直接接入大模型提示词。

关键改进 (v7.3)：
- 在 evaluate_one_defect 评估阶段即生成 PWHT 修复工艺建议。
  缺陷被判定为高风险而进入人工审核挂起时，audit 阶段尚未执行，
  提前生成建议可确保审核人员能够获取修复工艺指导。
- 保留 audit_one_defect 中的 PWHT 生成逻辑作为兜底（若评估阶段未生成）。

v7.2 历史功能（保留）：
- 修复 audit_one_defect 中风险等级字符串映射：适配全系统统一的四级风险等级
  （低=1，中=2，高=3，极高=4），解决高风险缺陷被错误分配等级的问题。
- 清理未使用的导入，修正注释中的 PVHT -> PWHT。

v7.1 历史功能（保留）：
- 新增缺陷类型归一化（normalize_defect_type），将 "表面裂纹"、"内部裂纹" 等
  别名统一映射为标准类型（如 "裂纹"），避免同一缺陷因表述不同被重复评估。
- 修复模块自测时缺少 `import json` 的运行时错误。

v7.0 历史功能（保留）：
- audit_one_defect 新增 PWHT（焊后热处理）修复工艺建议，基于
  GB/T 30583-2026 标准知识库，自动匹配材料组别并推荐：
    · 最低/最高保温温度
    · 最短保温时间（基于 δPWHT）
    · 升降温速率上限
    · 特殊材料附加要求（Fe-5B-2、Fe-9B、Fe-10I 等）
- _build_features 修复字段名映射（length_mm→length, depth_mm→depth）
- 新增材料牌号→NB/T 47014 组别映射表，支持常见承压设备用钢
- 所有 PWHT 集成均有异常保护，知识库不可用时不影响核心流程
- 保持原有 extract_defects / evaluate_one_defect / audit_one_defect 接口不变

v6.0 历史功能（保留）：
- extract_defects 直接调用新版 crews.extract_defects（返回 Pydantic 对象），
  并自动将 DefectBase.defect_type 映射为 rule_engine 期望的 'type' 字段。
- evaluate_one_defect 已适配新的扁平缺陷结构（无 dimensions 子对象），
  并传递上下文特征（介质、材质等）至知识库引擎，支持专家规则与相似案例检索。
- audit_one_defect 主动检索相似案例，提取措施建议合并至输出。
"""

import json
import logging
from typing import Dict, Any, List, Optional

# ⚠️ 注意：此处不导入 app.crews，避免与 crews.py 产生循环依赖。
# 需要在 extract_defects 函数内部延迟导入（见函数实现）。
# from app.crews import create_analysis_crew  # 已移除顶层导入

from app.core.utils import fmea_calculator, diagnosis_reasons
from app.core.regulation import search_regulation
from app.core.knowledge_base import get_knowledge_base

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# 缺陷类型归一化（v7.1 新增）
# ══════════════════════════════════════════════════════════════════════
DEFECT_TYPE_ALIASES: Dict[str, str] = {
    # ── 裂纹类 ──
    "表面裂纹": "裂纹",
    "内部裂纹": "裂纹",
    "纵向裂纹": "裂纹",
    "环向裂纹": "裂纹",
    "横向裂纹": "裂纹",
    "表面裂缝": "裂纹",
    "微裂纹": "裂纹",
    "龟裂": "裂纹",
    # ── 腐蚀类 ──
    "腐蚀减薄": "腐蚀",
    "均匀腐蚀": "腐蚀",
    "局部腐蚀": "腐蚀",
    "点腐蚀": "点蚀",
    "点状腐蚀": "点蚀",
    # ── 气孔类 ──
    "密集气孔": "气孔",
    "单个气孔": "气孔",
    "表面气孔": "气孔",
    # ── 夹杂类 ──
    "非金属夹杂": "夹杂",
    "夹渣": "夹杂",
    # ── 未熔合/未焊透类 ──
    "未焊透": "未焊透",
    "未熔合": "未熔合",
}


def normalize_defect_type(raw_type: str) -> str:
    """
    将 LLM 提取的缺陷类型字符串归一化为标准类型名称。

    规则：
      1. 去除首尾空白；
      2. 若在别名表中存在精确匹配，则返回标准名；
      3. 否则返回原字符串（避免信息丢失）。
    """
    if not raw_type:
        return "未知缺陷"

    cleaned = raw_type.strip()
    return DEFECT_TYPE_ALIASES.get(cleaned, cleaned)


# ══════════════════════════════════════════════════════════════════════
# PWHT 知识库惰性加载
# ══════════════════════════════════════════════════════════════════════
_pwht_kb_instance = None          # 缓存实例（None 表示未加载或加载失败）
_pwht_kb_attempted = False        # 是否已尝试加载


def _get_pwht_kb():
    """
    惰性获取 PWHT 标准知识库单例。

    首次调用时尝试导入并加载；若模块不存在或文件缺失，
    后续调用直接返回 None（功能降级，不影响核心评估流程）。
    """
    global _pwht_kb_instance, _pwht_kb_attempted

    if not _pwht_kb_attempted:
        _pwht_kb_attempted = True
        try:
            from app.core.pwht_knowledge_base import get_pwht_kb

            _pwht_kb_instance = get_pwht_kb()
            logger.info("✅ PWHT 标准知识库加载成功（GB/T 30583-2026）")
        except ImportError as e:
            logger.error(
                "⚠️ PWHT 知识库模块未安装（app/core/pwht_knowledge_base.py 不存在），"
                "修复工艺建议功能将禁用。错误: %s", e
            )
            _pwht_kb_instance = None
        except Exception as e:
            logger.error("⚠️ PWHT 知识库加载失败，修复工艺建议功能将禁用: %s", e)
            _pwht_kb_instance = None

    return _pwht_kb_instance


# ══════════════════════════════════════════════════════════════════════
# 材料牌号 → NB/T 47014 组别映射表
# ══════════════════════════════════════════════════════════════════════
# 匹配策略：按材料组别子类递减匹配，先精确后模糊。
# 钢牌号数字前缀可省略（如 "0Cr18Ni9" 可匹配 "06Cr19Ni10" 类）。

MATERIAL_GROUP_MAP: Dict[str, str] = {
    # ─────────── Fe-1 非合金钢（碳素钢） ───────────
    "Q235B":        "Fe-1-1",
    "Q235C":        "Fe-1-1",
    "Q235R":        "Fe-1-1",
    "Q245R":        "Fe-1-1",
    "20R":          "Fe-1-1",
    "20G":          "Fe-1-1",
    "20":           "Fe-1-1",
    "25":           "Fe-1-1",
    "S235JR":       "Fe-1-1",
    "SA516 Gr60":   "Fe-1-1",
    "SA516 Gr65":   "Fe-1-1",
    "SA516 Gr70":   "Fe-1-1",
    "A516 Gr60":    "Fe-1-1",
    "A516 Gr70":    "Fe-1-1",

    # Fe-1-2 低温用钢
    "09MnNiDR":     "Fe-1-2",
    "07MnNiDR":     "Fe-1-2",
    "SA203 GrD":    "Fe-1-2",
    "A203 GrD":     "Fe-1-2",

    # Fe-1-4 调质高强度钢
    "07MnMoVR":     "Fe-1-4",
    "07MnNiVDR":    "Fe-1-4",
    "07MnNiMoDR":   "Fe-1-4",
    "12MnNiVR":     "Fe-1-4",

    # ─────────── Fe-3 低合金钢 ───────────
    "Q345R":        "Fe-3-1",
    "16MnR":        "Fe-3-1",
    "16Mn":         "Fe-3-1",
    "Q370R":        "Fe-3-1",
    "15MnNbR":      "Fe-3-1",
    "16MnDR":       "Fe-3-1",
    "09MnD":        "Fe-3-1",
    "SA516 Gr55":   "Fe-3-1",
    "P355GH":       "Fe-3-1",

    # Fe-3-3 Cr-Mo 低合金钢
    "15CrMoR":      "Fe-3-3",
    "14Cr1MoR":     "Fe-3-3",

    # ─────────── Fe-4 Cr-Mo 耐热钢 ───────────
    "15CrMo":       "Fe-4-1",
    "12CrMo":       "Fe-4-1",
    "14Cr1Mo":      "Fe-4-1",
    "SA387 Gr12":   "Fe-4-1",
    "13CrMo4-5":    "Fe-4-1",

    "12Cr1MoV":     "Fe-4-2",
    "12Cr1MoVR":    "Fe-4-2",
    "12Cr2Mo1R":    "Fe-4-2",
    "12Cr2Mo1VR":   "Fe-5A",    # 高 Cr 等级归入 Fe-5A
    "SA387 Gr22":   "Fe-4-2",
    "10CrMo9-10":   "Fe-4-2",

    # ─────────── Fe-5A 高 Cr-Mo 钢 ───────────
    "SA336 F22V":   "Fe-5A",

    # ─────────── Fe-5B-1 中铬耐热钢 ───────────
    "SA387 Gr5":    "Fe-5B-1",

    # ─────────── Fe-5B-2 9Cr 马氏体耐热钢 ───────────
    "10Cr9Mo1VNbN":    "Fe-5B-2",
    "10Cr9MoW2VNbBN":  "Fe-5B-2",
    "T91":              "Fe-5B-2",
    "P91":              "Fe-5B-2",
    "T92":              "Fe-5B-2",
    "P92":              "Fe-5B-2",
    "SA213 T91":       "Fe-5B-2",
    "SA335 P91":       "Fe-5B-2",
    "AF91":             "Fe-5B-2",

    # ─────────── Fe-6 铁素体不锈钢 ───────────
    "06Cr13":       "Fe-6",
    "0Cr13":        "Fe-6",
    "10Cr17":       "Fe-6",
    "1Cr17":        "Fe-6",
    "022Cr12":      "Fe-6",

    # ─────────── Fe-7 奥氏体不锈钢（304 系列） ───────────
    "06Cr19Ni10":   "Fe-7",
    "0Cr18Ni9":     "Fe-7",
    "S30408":       "Fe-7",
    "022Cr19Ni10":  "Fe-7",
    "00Cr19Ni10":   "Fe-7",
    "S30403":       "Fe-7",
    "SA240 304":    "Fe-7",
    "A240 304":     "Fe-7",

    # ─────────── Fe-8 奥氏体不锈钢（316 系列等） ───────────
    "06Cr17Ni12Mo2":   "Fe-8",
    "0Cr17Ni12Mo2":    "Fe-8",
    "S31608":          "Fe-8",
    "022Cr17Ni12Mo2":  "Fe-8",
    "S31603":          "Fe-8",
    "SA240 316":       "Fe-8",
    "A240 316":        "Fe-8",

    # ─────────── Fe-9B 双相不锈钢 ───────────
    "S22053":       "Fe-9B",
    "S32205":       "Fe-9B",
    "S25073":       "Fe-9B",
    "S32750":       "Fe-9B",
    "022Cr23Ni5Mo3N": "Fe-9B",

    # ─────────── Fe-10I 超级铁素体不锈钢 ───────────
    "019Cr25Mo4Ni4NbTi": "Fe-10I",
    "S12562":           "Fe-10I",

    # ─────────── Fe-11A 镍基合金 ───────────
    "N06600":       "Fe-11A",
    "Inconel 600":  "Fe-11A",
    "N06625":       "Fe-11A",
    "Inconel 625":  "Fe-11A",
    "N08800":       "Fe-11A",
    "Incoloy 800":  "Fe-11A",
    "N04400":       "Fe-11A",
    "Monel 400":    "Fe-11A",
}

# ─────────── 焊接类缺陷类型（需要 PWHT 修复工艺建议） ───────────
WELD_REPAIR_DEFECT_TYPES = [
    "裂纹", "表面裂纹", "内部裂纹", "微裂纹",
    "气孔", "夹渣", "夹杂",
    "未焊透", "未熔合", "咬边", "焊瘤",
]


def _map_material_to_group(material: str) -> Optional[str]:
    """
    将材料牌号字符串映射到 NB/T 47014 材料组别（如 "Fe-4-2"）。

    匹配策略：
      1. 精确匹配：材料字符串中包含映射表中某个牌号（按牌号长度降序尝试，
         避免 "20" 过早匹配 "20G"）。
      2. 若精确匹配失败，尝试提取材料中的主要牌号片段再匹配。
      3. 全部失败返回 None。

    Args:
        material: 材料牌号（如 "Q345R"、"10Cr9Mo1VNbN"、"Q345R(板厚30mm)"）

    Returns:
        材料组别 ID（如 "Fe-3-1"），无法识别为 None
    """
    if not material:
        return None

    # 清理常见修饰词
    cleaned = material.strip()

    # 策略 1：按映射键长度降序逐一检查
    for key in sorted(MATERIAL_GROUP_MAP.keys(), key=len, reverse=True):
        if key.lower() in cleaned.lower():
            return MATERIAL_GROUP_MAP[key]

    # 策略 2：提取钢牌号模式（如 QxxxR、xxCrxx 等）再做匹配
    # 简单起见：截取前 20 字符处理
    short = cleaned[:20]
    for key in sorted(MATERIAL_GROUP_MAP.keys(), key=len, reverse=True):
        # 去除可能的细化标记后仅比较核心部分（取前 N 字符）
        core_len = min(len(key), 4)
        if key[:core_len].lower() == short[:core_len].lower():
            return MATERIAL_GROUP_MAP[key]

    return None


# ══════════════════════════════════════════════════════════════════════
# 辅助：将 DefectBase 字段转换为规则引擎兼容的扁平字典
# ══════════════════════════════════════════════════════════════════════
def _normalize_defect_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 crews 提取的原始缺陷字典转换为下游函数统一使用的格式：
      - 'defect_type' → 'type'（规则引擎期望的字段名）
      - 应用缺陷类型归一化（表面裂纹 → 裂纹）
      - 保留顶层 length / depth / wall_thickness / quantity 等字段
      - 若原始数据存在嵌套的 'dimensions' 对象（旧版兼容），则自动展平
    """
    defect = raw.copy()

    # 1. 字段重命名：defect_type -> type
    if "defect_type" in defect and "type" not in defect:
        defect["type"] = defect.pop("defect_type")
    # 若都没有，设默认值
    defect.setdefault("type", "未知缺陷")

    # ★ v7.1 新增：缺陷类型归一化（如 "表面裂纹" → "裂纹"）
    defect["type"] = normalize_defect_type(defect["type"])

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

    # 4. 统一上下文字段缺失时的默认值为空字符串或 None
    for field in ("media", "material", "device_type", "environment", "location"):
        if field not in defect:
            defect[field] = ""  # 空字符串便于后续判断

    for field in ("operating_temperature", "design_pressure"):
        if field not in defect:
            defect[field] = None

    return defect


# ══════════════════════════════════════════════════════════════════════
# 辅助：构建用于知识库检索的特征字典
# ══════════════════════════════════════════════════════════════════════
def _build_features(defect: Dict[str, Any]) -> Dict[str, Any]:
    """
    从缺陷字典中提取知识库检索所需的特征。
    仅包含有意义的非空值，避免干扰匹配。

    v7.0 修复：length_mm / depth_mm 字段名统一为规范的 length / depth。
    """
    # 字段名映射：知识库取值字段 → 缺陷字典实际键名
    fields = [
        ("defect_type",  "type"),
        ("media",        "media"),
        ("material",     "material"),
        ("device_type",  "device_type"),
        ("environment",  "environment"),
        ("operating_temperature", "operating_temperature"),
        ("design_pressure",       "design_pressure"),
        ("location",     "location"),
        ("length_mm",    "length"),       # ★ 修复：正确映射
        ("depth_mm",     "depth"),        # ★ 修复：正确映射
        ("wall_thickness", "wall_thickness"),
    ]

    features = {}
    for kb_key, defect_key in fields:
        val = defect.get(defect_key, None)
        # 过滤 None 和空字符串
        if val is not None and val != "":
            # 数值字段转换为 float
            if defect_key in ("length", "depth", "wall_thickness"):
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
            features[kb_key] = val
    return features


# ══════════════════════════════════════════════════════════════════════
# PWHT 修复工艺建议辅助函数
# ══════════════════════════════════════════════════════════════════════
def _is_weld_repair_defect(defect_type: str) -> bool:
    """
    判断缺陷类型是否属于焊接类（需要补焊修复→可能需要 PWHT）。
    """
    if not defect_type:
        return False
    return any(keyword in defect_type for keyword in WELD_REPAIR_DEFECT_TYPES)


def _calc_holding_time(
    delta_pwht: float,
    material_group: str,
    equipment_type: str = "vessel",
) -> float:
    """
    根据 GB/T 30583-2026 表1/表2 计算最短保温时间。

    采用锅炉表1的通用三段式公式（适用于绝大多数材料组别）：
      - δPWHT ≤ 50mm:    max(δPWHT/25, 0.25h)     [Fe-5B-2 为 0.5h]
      - 50 < δPWHT ≤ 125mm: 2 + (δPWHT-50)/100
      - δPWHT > 125mm:   5 + (δPWHT-125)/100

    压力容器表2在 25-50mm 分段与锅炉表略有差异，
    此处采用保守策略统一按锅炉三段式处理并注明。
    """
    # Fe-5B-2 特殊：≤50mm 时最少 0.5h
    min_first_segment = 0.5 if material_group == "Fe-5B-2" else 0.25

    if delta_pwht <= 50:
        return max(delta_pwht / 25.0, min_first_segment)
    elif delta_pwht <= 125:
        return 2.0 + (delta_pwht - 50.0) / 100.0
    else:
        return 5.0 + (delta_pwht - 125.0) / 100.0


def _get_pwht_repair_advice(
    defect: Dict[str, Any],
    level_num: int,
) -> Optional[Dict[str, Any]]:
    """
    为焊接类缺陷生成 PWHT 修复工艺建议（基于 GB/T 30583-2026）。

    仅在以下条件同时满足时给出建议：
      1. 缺陷类型属于焊接修复类（裂纹/气孔/夹渣/未焊透等）
      2. 缺陷风险等级达到中风险及以上（level_num ≥ 2）
      3. 缺陷包含材料牌号信息
      4. PWHT 知识库已加载成功

    Returns:
        pwht_advice 字典；不适用时返回 None
    """
    defect_type = defect.get("type", "")
    material = defect.get("material", "")
    wall_thickness = defect.get("wall_thickness")

    # 条件 1：焊接类缺陷
    if not _is_weld_repair_defect(defect_type):
        return None

    # 条件 2：需要修复干预（中风险及以上）
    if level_num < 2:
        return None

    # 条件 3：材料已知
    if not material:
        logger.debug("缺陷 %s 缺少材料牌号，无法生成 PWHT 建议", defect.get("id"))
        return None

    # 映射材料组别
    material_group = _map_material_to_group(material)
    if not material_group:
        logger.debug("材料牌号 '%s' 无法映射到 NB/T 47014 组别", material)
        return {
            "applicable": False,
            "standard": "GB/T 30583-2026",
            "warning": (
                f"材料牌号 '{material}' 未在已知映射表中，"
                "建议人工查阅 GB/T 30583-2026 第4.4节确定 PWHT 参数"
            ),
        }

    # 条件 4：知识库可用
    kb = _get_pwht_kb()
    if kb is None:
        return None

    # 确定 δPWHT：优先使用 wall_thickness（等厚对接接头近似）
    delta_pwht = wall_thickness if wall_thickness else None

    advice: Dict[str, Any] = {
        "applicable": True,
        "standard": "GB/T 30583-2026",
        "material_group": material_group,
        "material": material,
        "repair_context": "焊接修复后应参照本标准进行焊后热处理",
        "notes": [],
    }

    # ── 获取推荐保温温度范围 ──
    try:
        params = kb.get_recommended_params(
            material_group=material_group,
            equipment_type="vessel",  # 默认压力容器；锅炉可在后续扩展
            delta_pwht=delta_pwht,
        )
        if params.get("error"):
            advice["applicable"] = False
            advice["error"] = params["error"]
            return advice

        advice["min_holding_temp_c"] = params.get("min_holding_temp_c")
        advice["max_holding_temp_c"] = params.get("max_holding_temp_c")
        if "min_holding_time_h" in params:
            advice["min_holding_time_h"] = round(params["min_holding_time_h"], 2)
        if delta_pwht:
            advice["delta_pwht_mm"] = delta_pwht
    except Exception as e:
        logger.error("PWHT 参数查询失败: %s", e)
        advice.setdefault("notes", []).append("保温温度查询异常，请人工确认")

    # ── 获取升降温速率限值 ──
    if delta_pwht and delta_pwht > 0:
        try:
            limits = kb.get_heating_cooling_limits(
                delta_pwht=delta_pwht,
                is_boiler=False,
            )
            advice["entry_temp_max_c"] = limits.get("entry_temp_max_c")
            advice["heating_rate_max_c_per_h"] = limits.get("heating_rate_max_c_per_h")
            advice["cooling_rate_max_c_per_h"] = limits.get("cooling_rate_max_c_per_h")
            advice["soaking_max_temp_diff_c"] = limits.get("soaking_max_temp_diff_c")
        except Exception as e:
            logger.error("PWHT 速率限值查询失败: %s", e)
            advice.setdefault("notes", []).append("升降温速率查询异常，请人工确认")

    # ── 检查特殊材料附加要求 ──
    try:
        specials = kb.check_special_materials(material_group)
        for spec in specials:
            advice.setdefault("special_requirements", []).append({
                "title": spec.get("title", ""),
                "section": spec.get("section", ""),
                "note": str(spec.get("requirements", ""))[:200],  # 截断避免过长
            })
    except Exception as e:
        logger.error("PWHT 特殊材料检查失败: %s", e)

    # ── 高合金钢冷却注意事项 ──
    high_alloy = ["Fe-6", "Fe-7", "Fe-8", "Fe-10I", "Fe-10H", "Fe-11A"]
    if any(g in material_group for g in high_alloy):
        advice.setdefault("notes", []).append(
            "高合金钢焊后热处理应参照 GB/T 30583-2026 第4.4.15 f)条款"
            "关注冷却速率和防脆化要求"
        )

    # ── 标准引用条款备注 ──
    advice.setdefault("notes", []).append(
        "具体保温时间分段和降温补偿条件见 GB/T 30583-2026 表1/表2/表3"
    )

    if not advice.get("notes"):
        advice.pop("notes", None)

    return advice


# ══════════════════════════════════════════════════════════════════════
# 新增：构建 LLM 上下文字符串（v7.4）
# ══════════════════════════════════════════════════════════════════════
def build_llm_context(defect_data: Dict[str, Any]) -> str:
    """
    将缺陷结果字典（通常是 audit_one_defect 的输出）格式化为
    可直接嵌入大模型提示词的综合上下文文本。

    该文本涵盖：
      - 缺陷基本信息（类型、位置、尺寸等）
      - FMEA 评级（RPN、风险等级）
      - 失效原因
      - 法规要求与强制措施
      - 检验建议
      - 相似案例整改措施（重点整合）
      - PWHT 建议（若有）

    Args:
        defect_data: 包含完整评估与审计结果的缺陷字典。

    Returns:
        str: 多段文本，可直接插入 prompt。
    """
    def _fmt_value(val, prefix=""):
        if val is None or val == "":
            return "无"
        if isinstance(val, list):
            if not val:
                return "无"
            return "\n".join(f"{prefix}- {item}" for item in val)
        return str(val)

    lines = []
    lines.append("【缺陷基本信息】")
    lines.append(f"类型：{defect_data.get('type', '未知')}")
    lines.append(f"位置：{defect_data.get('location', '未知')}")
    lines.append(f"尺寸：长 {defect_data.get('length', '?')} mm，深 {defect_data.get('depth', '?')} mm")
    lines.append(f"壁厚：{defect_data.get('wall_thickness', '?')} mm")
    lines.append(f"数量：{defect_data.get('quantity', 1)}")
    lines.append(f"介质：{defect_data.get('media', '未知')}")
    lines.append(f"材质：{defect_data.get('material', '未知')}")

    lines.append("\n【风险评估】")
    lines.append(f"RPN：{defect_data.get('rpn', '无')}")
    lines.append(f"风险等级：{defect_data.get('risk_level', '未知')} (level={defect_data.get('level', '?')})")

    lines.append("\n【失效原因分析】")
    reasons = defect_data.get('reasons', [])
    if reasons:
        for idx, reason in enumerate(reasons, 1):
            lines.append(f"{idx}. {reason}")
    else:
        lines.append("无")

    lines.append("\n【法规与强制措施】")
    lines.append(f"法规引用：{_fmt_value(defect_data.get('law_references'))}")
    lines.append(f"强制措施：{_fmt_value(defect_data.get('mandatory_measures'))}")
    lines.append(f"检验建议：{_fmt_value(defect_data.get('inspection_advice'))}")

    lines.append("\n【同类事故案例整改措施】")
    measures = defect_data.get('similar_case_measures', [])
    if measures:
        for measure in measures:
            lines.append(f"- {measure}")
    else:
        lines.append("无")

    pwht = defect_data.get('pwht_advice')
    if pwht:
        lines.append("\n【PWHT 修复工艺建议】")
        if pwht.get('applicable', False):
            lines.append(f"材料：{pwht.get('material', '')}")
            lines.append(f"材料组别：{pwht.get('material_group', '')}")
            lines.append(f"最低保温温度：{pwht.get('min_holding_temp_c', '无')} °C")
            lines.append(f"最高保温温度：{pwht.get('max_holding_temp_c', '无')} °C")
            lines.append(f"最短保温时间：{pwht.get('min_holding_time_h', '无')} h")
            if 'delta_pwht_mm' in pwht:
                lines.append(f"δPWHT：{pwht['delta_pwht_mm']} mm")
            if pwht.get('heating_rate_max_c_per_h'):
                lines.append(f"升温速率上限：{pwht['heating_rate_max_c_per_h']} °C/h")
            if pwht.get('cooling_rate_max_c_per_h'):
                lines.append(f"降温速率上限：{pwht['cooling_rate_max_c_per_h']} °C/h")
            if pwht.get('special_requirements'):
                lines.append("特殊要求：")
                for req in pwht['special_requirements']:
                    lines.append(f"  - {req.get('title', '')}: {req.get('note', '')[:100]}")
            if pwht.get('notes'):
                lines.append("备注：")
                for note in pwht['notes']:
                    lines.append(f"  - {note}")
        else:
            lines.append(f"不适用（{pwht.get('warning', '') or pwht.get('error', '')}）")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 公共接口
# ══════════════════════════════════════════════════════════════════════
def extract_defects(input_text: str) -> List[Dict[str, Any]]:
    """
    从非结构化检验报告中提取缺陷列表（仅一次 LLM 调用）。

    Returns:
        List[dict] : 每个字典为已规范化的缺陷数据，包含 'type' 字段等。
    Raises:
        ParsingError: 当 LLM 输出无法通过 schema 校验时。
        LLMTimeoutError / LLMAPIError: 当 LLM 接口调用失败时。
    """
    # ★ 延迟导入，避免与 crews.py 的循环依赖
    from app.crews import create_analysis_crew

    # 调用新版 extract_defects（已在 crews 中通过 output_pydantic 强制校验）
    pydantic_result = create_analysis_crew(input_text)       # -> DefectExtractionResult
    raw_defects = pydantic_result.defects                    # List[DefectBase]

    if not raw_defects:
        logger.info("提取结果：未发现任何缺陷。")
        return []

    # 转为字典并规范字段（含类型归一化）
    normalized = []
    for item in raw_defects:
        d = _normalize_defect_dict(item.model_dump())
        normalized.append(d)

    logger.info("成功提取 %d 条缺陷记录。", len(normalized))
    return normalized


def evaluate_one_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
    """
    对单条缺陷执行 FMEA 评级 + 失效原因诊断（规则引擎 + 知识库增强）。

    期望的输入缺陷字典已经过 _normalize_defect_dict 处理，包含 'type'、'length'、
    'depth'、'wall_thickness' 以及介质、材质等上下文字段。若字段缺失，
    规则引擎与知识库将采用保守策略（壁厚缺失时尝试案例库降级）。

    v7.3 新增：评估阶段即生成 PWHT 修复工艺建议（若适用），
    确保高风险缺陷被迫挂起人工审核时，审核人员也能获取建议。

    Returns:
        dict : 原始缺陷信息 + fmea 评分 (severity, occurrence, detection, rpn,
               risk_level, level, triggered_rules, rule_applications,
               similar_cases, similar_case_ids, similar_case_measures 等)
               + reasons + pwht_advice（若适用，否则为 null）
    """
    # 确保基础字段
    defect = _normalize_defect_dict(defect)   # 二次保险（幂等）

    dtype = defect.get("type", "")
    length = defect.get("length")            # 可能为 None
    depth = defect.get("depth")              # 可能为 None
    wall_thickness = defect.get("wall_thickness")
    quantity = defect.get("quantity", 1)

    # 提取上下文特征
    media = defect.get("media", "")
    material = defect.get("material", "")
    device_type = defect.get("device_type", "")
    environment = defect.get("environment", "")
    operating_temperature = defect.get("operating_temperature")
    design_pressure = defect.get("design_pressure")
    location = defect.get("location", "")

    logger.debug("评估缺陷: type=%s, length=%s, depth=%s, wall=%s, qty=%s, media=%s, device=%s",
                 dtype, length, depth, wall_thickness, quantity, media, device_type)

    # 调用核心规则引擎（已集成知识库：专家规则调整 + 相似案例检索）
    fmea_result = fmea_calculator(
        defect_type=dtype,
        length_mm=length,
        depth_mm=depth,
        wall_thickness=wall_thickness,
        quantity=quantity,
        media=media,
        material=material,
        device_type=device_type,
        environment=environment,
        operating_temperature=operating_temperature,
        design_pressure=design_pressure,
        location=location,
    )

    # 失效原因诊断（增强版会提取案例根因）
    reasons = diagnosis_reasons(dtype, media=media, material=material,
                                device_type=device_type, environment=environment,
                                location=location)

    # ★ v7.3 新增：在评估阶段提前生成 PWHT 修复工艺建议
    pwht_advice = None
    try:
        level_num = fmea_result.get("level")
        if level_num is not None:
            pwht_advice = _get_pwht_repair_advice(defect, int(level_num))
    except Exception as e:
        logger.error("评估阶段 PWHT 建议生成失败: %s，跳过。", e)
        pwht_advice = None

    # 合并结果（fmea_result 中的字段覆盖原始字段，例如 severity 等）
    combined = {**defect, **fmea_result, "reasons": reasons, "pwht_advice": pwht_advice}
    logger.info("缺陷评估完成: id=%s, RPN=%s, risk_level=%s, pwht_advice=%s",
                defect.get("id"), fmea_result.get("rpn"),
                fmea_result.get("risk_level"),
                "已生成" if pwht_advice else "未生成")
    return combined


def audit_one_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
    """
    为单条缺陷检索适用的法规条文、检验建议，并补充相似案例措施。

    优先使用 numerical level（1-4），若不存在则根据中文风险等级字符串推断。
    当风险等级无法确定时，标记为“需人工判定”，避免错误调用法规。
    同时调用知识库检索相似案例，提取维修/检验措施建议。

    v7.0 新增：对于焊接类缺陷（裂纹、气孔、夹渣、未焊透、未熔合等），
    自动生成 PWHT（焊后热处理）修复工艺建议，包含推荐保温温度、
    最短保温时间、升降温速率限值及特殊材料要求。
    （若评估阶段已生成建议，则审计阶段将重新生成以确保最新数据有效性）

    v7.4 新增：在结果中添加 llm_context 字段，整合全部信息，
    可直接用于大模型生成综合评估报告。

    Returns:
        dict : 原始缺陷字典 + law_references / mandatory_measures / inspection_advice
               + similar_case_measures（可能重复，若已存在于缺陷中则覆盖）
               + similar_cases / similar_case_ids 等（若之前未生成）
               + pwht_advice（v7.0 新增，不适用时为 None）
               + llm_context（v7.4 新增，格式化文本）
    """
    defect = _normalize_defect_dict(defect)   # 确保字段一致

    dtype = defect.get("type", "")

    # ------------------------------------------------------------------
    # 1. 确定数值风险等级
    # ------------------------------------------------------------------
    level_num = defect.get("level")            # rule_engine 输出的数值等级
    if level_num is None:
        risk_str = defect.get("risk_level", "")
        # ★ v7.2 修正：适配全系统统一的四级风险等级
        #   低=1，中=2，高=3，极高=4
        #   注意：必须先判断“极高”，避免被“高”抢先匹配
        if "极高" in risk_str or "严重" in risk_str:
            level_num = 4
        elif "高" in risk_str:
            level_num = 3
        elif "中" in risk_str:
            level_num = 2
        elif "低" in risk_str:
            level_num = 1
        else:
            level_num = 0   # 无法识别

    # ------------------------------------------------------------------
    # 2. 结构化调用法规检索
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 3. 相似案例检索（若知识库可用）
    # ------------------------------------------------------------------
    similar_case_measures = []
    similar_cases = []
    similar_case_ids = []

    # 尝试获取知识库实例（可能失败，需安全处理）
    try:
        kb = get_knowledge_base()
        # 构建特征
        features = _build_features(defect)
        if features:
            similar = kb.search_similar_cases(features, top_k=5)
            similar_cases = similar
            similar_case_ids = [item["case_id"] for item in similar]
            similar_case_measures = [
                measure
                for item in similar
                for measure in item["measures"]
            ]
            # 去重并保持顺序
            similar_case_measures = list(dict.fromkeys(similar_case_measures))
        else:
            logger.debug("无有效特征用于相似案例检索")
    except Exception as e:
        logger.error("相似案例检索失败: %s，跳过。", e)
        # 如果缺陷中已有之前步骤的结果，则保留
        similar_case_measures = defect.get("similar_case_measures", [])
        similar_cases = defect.get("similar_cases", [])
        similar_case_ids = defect.get("similar_case_ids", [])

    # ------------------------------------------------------------------
    # 4. PWHT 修复工艺建议（v7.0 新增）
    #    注：评估阶段可能已生成；此处重新生成以保证结果最新。
    # ------------------------------------------------------------------
    pwht_advice = None
    try:
        pwht_advice = _get_pwht_repair_advice(defect, level_num)
        if pwht_advice is not None:
            logger.info(
                "缺陷 %s 已生成 PWHT 修复工艺建议（material=%s, group=%s）",
                defect.get("id"),
                defect.get("material", ""),
                pwht_advice.get("material_group", ""),
            )
    except Exception as e:
        logger.error("PWHT 修复工艺建议生成失败: %s，跳过。", e)
        pwht_advice = None

    # ------------------------------------------------------------------
    # 5. 合并结果
    # ------------------------------------------------------------------
    result = defect.copy()
    result["law_references"] = law_info.get("law_references", "")
    result["mandatory_measures"] = law_info.get("mandatory_measures", "")
    result["inspection_advice"] = law_info.get("inspection_advice", "")
    # 覆盖/补充相似案例相关字段
    result["similar_cases"] = similar_cases
    result["similar_case_ids"] = similar_case_ids
    result["similar_case_measures"] = similar_case_measures
    # v7.0 新增：PWHT 修复工艺建议
    result["pwht_advice"] = pwht_advice

    # ★ v7.4 新增：构建 LLM 上下文字符串
    result["llm_context"] = build_llm_context(result)

    return result


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("缺陷处理器 v7.4 自测")
    print("=" * 60)

    # ── 测试 1：材料映射 ──
    print("\n── 测试 1：材料组别映射 ──")
    test_materials = ["Q345R", "12Cr1MoV", "10Cr9Mo1VNbN", "S30408", "未知材料"]
    for mat in test_materials:
        group = _map_material_to_group(mat)
        print(f"  {mat:20s} → {group or '无法映射'}")

    # ── 测试 2：焊接缺陷判断 ──
    print("\n── 测试 2：焊接缺陷判断 ──")
    test_types = ["表面裂纹", "点蚀", "气孔", "变形", "夹渣", "磨损"]
    for t in test_types:
        print(f"  {t:10s} → 焊接修复类: {_is_weld_repair_defect(t)}")

    # ── 测试 3：缺陷类型归一化 ──
    print("\n── 测试 3：缺陷类型归一化 ──")
    test_raw_types = ["表面裂纹", "内部裂纹", "点腐蚀", "密集气孔", "夹渣", "未知类型"]
    for t in test_raw_types:
        norm = normalize_defect_type(t)
        print(f"  {t:10s} → {norm}")

    # ── 测试 4：PWHT 参数建议 ──
    print("\n── 测试 4：PWHT 建议生成 ──")
    test_defect = {
        "id": 1,
        "type": "裂纹",
        "material": "12Cr1MoV",
        "wall_thickness": 30.0,
        "length": 50.0,
        "depth": 2.0,
        "level": 3,
        "risk_level": "高风险",  # 注意：四级体系中 level=3 对应高风险
        "quantity": 1,
        "media": "",
        "device_type": "",
        "environment": "",
        "location": "筒体环焊缝",
        "original_text": "筒体环焊缝发现表面裂纹长50mm",
    }
    advice = _get_pwht_repair_advice(test_defect, 3)
    print(json.dumps(advice, ensure_ascii=False, indent=2) if advice else "  无建议")

    # ── 测试 5：LLM 上下文生成 ──
    print("\n── 测试 5：LLM 上下文生成 ──")
    sample_result = {
        "type": "裂纹",
        "location": "筒体环焊缝",
        "length": 50.0,
        "depth": 2.0,
        "wall_thickness": 30.0,
        "quantity": 1,
        "media": "天然气",
        "material": "Q345R",
        "rpn": 120,
        "risk_level": "高风险",
        "level": 3,
        "reasons": ["焊接残余应力集中", "材料韧性不足"],
        "law_references": "TSG 21-2016 第4.2.3条",
        "mandatory_measures": "立即停止使用，进行修复",
        "inspection_advice": "建议进行超声检测",
        "similar_case_measures": ["打磨消除裂纹后补焊", "进行焊后热处理"],
        "pwht_advice": {
            "applicable": True,
            "material": "Q345R",
            "material_group": "Fe-3-1",
            "min_holding_temp_c": 600,
            "max_holding_temp_c": 650,
            "min_holding_time_h": 1.2,
            "delta_pwht_mm": 30.0,
            "heating_rate_max_c_per_h": 50,
            "cooling_rate_max_c_per_h": 60,
        },
    }
    context = build_llm_context(sample_result)
    print(context)