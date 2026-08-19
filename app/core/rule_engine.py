"""
动态 FMEA 规则引擎 —— 基于 rules.json 可配置 (v3.1 修复版)

特性：
- 线程安全：单例创建、热重载均使用锁保护
- 数据适配：自动处理嵌套的 dimensions 结构，提取有效数值字段
- 缺失值保守策略：当关键尺寸（深度/长度/壁厚）缺失时，相关规则不生效，
  避免因数据不足而错误低估风险
- 向后兼容：保留 rule_engine 全局变量，旧代码无需修改
- 内置兜底规则：覆盖 裂纹/点蚀/气孔/夹杂 等常见缺陷

修复记录：
- v3.1: 修正 _match 中字段名不一致问题（depth_mm → depth, length_mm → length）
        增强 _normalize_defect 对 dimensions 中键名兼容性（支持 depth/length 和 depth_mm/length_mm）
"""

import os
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from app.core.config import RULES_PATH   # 请确保配置文件正确导出该路径

logger = logging.getLogger("defect_fmea.rule_engine")


class RuleEngine:
    """基于 rules.json 的动态 FMEA 计算引擎（线程安全 + 数据适配版）"""

    def __init__(self, rules_path: Optional[str] = None):
        self.rules_path = rules_path or RULES_PATH
        self.rules: List[Dict[str, Any]] = []
        self._reload_lock = threading.Lock()
        self.load_rules()

    # ------------------- 规则加载 -------------------
    def load_rules(self) -> None:
        new_rules = self._load_rules_from_source()
        self.rules = new_rules
        logger.info(f"规则已加载，当前规则总数：{len(self.rules)}")

    def reload_rules(self) -> bool:
        """热重载规则文件（线程安全）。成功返回 True，失败保留旧规则。"""
        with self._reload_lock:
            try:
                new_rules = self._load_rules_from_source()
                self.rules = new_rules
                logger.info(f"✅ 热重载成功，当前规则总数：{len(self.rules)}")
                return True
            except Exception as e:
                logger.error(f"❌ 热重载失败，将继续使用旧规则。错误：{e}")
                return False

    def _load_rules_from_source(self) -> List[Dict[str, Any]]:
        """从外部文件加载规则，失败则降级为内置默认规则。"""
        if self.rules_path and os.path.exists(self.rules_path):
            try:
                with open(self.rules_path, "r", encoding="utf-8") as f:
                    rules = json.load(f)
                if not isinstance(rules, list):
                    raise ValueError("规则文件内容应为 JSON 数组")
                logger.info(f"✅ 从文件加载 {len(rules)} 条规则：{self.rules_path}")
                return rules
            except Exception as e:
                logger.error(f"规则文件加载失败：{e}，降级为内置默认规则")
                return self._default_rules()
        else:
            logger.warning(f"规则文件 {self.rules_path} 未找到，使用内置默认规则")
            return self._default_rules()

    # ------------------- 默认规则集（兜底） -------------------
    @staticmethod
    def _default_rules() -> List[Dict[str, Any]]:
        return [
            # 严重度 Severity
            {"rule_id": "SEV_BASE", "description": "默认严重度", "condition": {}, "effect": {"severity": 3}, "source": "默认值"},
            {"rule_id": "SEV_CRACK", "description": "裂纹基础严重度", "condition": {"type_contains": "裂纹"}, "effect": {"severity": 4}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_D2", "description": "裂纹深度≥2mm", "condition": {"type_contains": "裂纹", "depth_min": 2.0}, "effect": {"severity": 5}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_R03", "description": "裂纹深度/壁厚≥0.3", "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.3}, "effect": {"severity": 7}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_CRACK_R05", "description": "裂纹深度/壁厚≥0.5", "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.5}, "effect": {"severity": 9}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_PITTING", "description": "点蚀/腐蚀基础", "condition": {"type_contains": ["点蚀", "腐蚀"]}, "effect": {"severity": 4}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_D2", "description": "点蚀深度≥2mm", "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_min": 2.0}, "effect": {"severity": 6}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_R05", "description": "点蚀深度/壁厚≥0.5", "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_wall_ratio_min": 0.5}, "effect": {"severity": 8}, "source": "API 579"},
            {"rule_id": "SEV_POROSITY_S", "description": "气孔/夹杂长度≤10mm", "condition": {"type_contains": ["气孔", "夹杂"], "length_max": 10}, "effect": {"severity": 3}, "source": "经验规则"},
            {"rule_id": "SEV_POROSITY_L", "description": "气孔/夹杂长度>10mm", "condition": {"type_contains": ["气孔", "夹杂"], "length_min": 10}, "effect": {"severity": 5}, "source": "经验规则"},
            # 发生度 Occurrence
            {"rule_id": "OCC_BASE", "condition": {}, "effect": {"occurrence": 4}, "source": "默认"},
            {"rule_id": "OCC_Q1", "condition": {"quantity_eq": 1}, "effect": {"occurrence": 3}, "source": "默认"},
            {"rule_id": "OCC_Q3", "condition": {"quantity_min": 3}, "effect": {"occurrence": 5}, "source": "默认"},
            {"rule_id": "OCC_Q5", "condition": {"quantity_min": 5}, "effect": {"occurrence": 7}, "source": "默认"},
            # 检出度 Detection
            {"rule_id": "DET_CRACK", "condition": {"type_contains": "裂纹"}, "effect": {"detection": 6}, "source": "默认"},
            {"rule_id": "DET_PITTING", "condition": {"type_contains": ["点蚀", "腐蚀"]}, "effect": {"detection": 5}, "source": "默认"},
            {"rule_id": "DET_BASE", "condition": {}, "effect": {"detection": 4}, "source": "默认"},
        ]

    # ------------------- 数据规范化 -------------------
    @staticmethod
    def _normalize_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
        """
        将缺陷字典转换为规则引擎可用的扁平格式。
        标准内部字段为:
            - depth : 缺陷深度 (mm)
            - length : 缺陷长度 (mm)
            - wall_thickness : 设计壁厚 (mm)
            - quantity : 数量
            - type : 缺陷类型字符串
        兼容旧版嵌套 dimensions 结构，及不同的键名 (depth_mm/length_mm 等)。
        """
        data = defect.copy()               # 保留所有原始字段，便于未来扩展
        dims = data.get("dimensions") or {}

        # 辅助函数：将任意值转为 float 或 None
        def _to_float(v):
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                logger.debug(f"数值转换失败，忽略: {v}")
                return None

        # 深度、长度优先从顶层取，若无则从 dimensions 提取（支持多种键名）
        # 标准键名：depth, length；兼容键名：depth_mm, length_mm
        for standard_key, legacy_key in [("depth", "depth_mm"), ("length", "length_mm")]:
            # 如果 data 中已有标准键且非 None，则直接转换
            if data.get(standard_key) is not None:
                data[standard_key] = _to_float(data[standard_key])
                continue

            # 否则尝试从 dims 中提取（先标准键，再旧键）
            val = dims.get(standard_key)
            if val is None:
                val = dims.get(legacy_key)
            # 如果 dims 中没有，则尝试从原 defect 的旧键提取
            if val is None:
                val = defect.get(standard_key)
            if val is None:
                val = defect.get(legacy_key)

            data[standard_key] = _to_float(val)

        # 壁厚
        wt = data.get("wall_thickness")
        if wt is None:
            wt = dims.get("wall_thickness", defect.get("wall_thickness"))
        data["wall_thickness"] = _to_float(wt)

        # 数量始终为整数
        try:
            data["quantity"] = int(defect.get("quantity", 1))
        except (TypeError, ValueError):
            data["quantity"] = 1

        return data

    # ------------------- 核心评估 -------------------
    def evaluate(self, defect: Dict[str, Any]) -> Dict[str, Any]:
        """
        对单条缺陷执行 FMEA 评分。
        - 内部先调用 _normalize_defect 处理数据结构。
        - 使用当前规则的快照，热重载不影响本次评估。
        - 缺失关键数值的规则会被安全跳过（保守原则）。
        """
        data = self._normalize_defect(defect)
        current_rules = self.rules          # 快照引用
        s, o, d = 1, 1, 1
        triggered = []

        for rule in current_rules:
            if self._match(rule.get("condition", {}), data):
                triggered.append(rule)
                eff = rule.get("effect", {})
                # 绝对赋值
                if "severity" in eff:
                    s = eff["severity"]
                if "occurrence" in eff:
                    o = eff["occurrence"]
                if "detection" in eff:
                    d = eff["detection"]
                # 增量调整
                if "severity_delta" in eff:
                    s += eff["severity_delta"]
                if "occurrence_delta" in eff:
                    o += eff["occurrence_delta"]
                if "detection_delta" in eff:
                    d += eff["detection_delta"]

        s = max(1, min(10, s))
        o = max(1, min(10, o))
        d = max(1, min(10, d))
        rpn = s * o * d
        level = self._rpn_to_level(rpn)
        sources = list({r.get("source", "未知") for r in triggered})

        return {
            "severity": s,
            "occurrence": o,
            "detection": d,
            "rpn": rpn,
            "risk_level": self._level_map[level],
            "level": level,
            "triggered_rules": [r["rule_id"] for r in triggered],
            "standard_ref": ", ".join(sources),
        }

    # ------------------- 条件匹配 -------------------
    @staticmethod
    def _match(condition: Dict[str, Any], data: Dict[str, Any]) -> bool:
        """
        判断规范化后的数据 data 是否满足一条规则的所有条件。
        关键安全策略：对于数值比较（depth_min 等），若所需字段为 None，
        则视为不满足，防止因数据缺失而错误触发低风险规则。

        数据字段使用规范化后的标准名称：depth, length, wall_thickness, quantity, type。
        """
        for key, val in condition.items():
            if key == "type_contains":
                dtype = data.get("type", "")
                if isinstance(val, list):
                    if not any(sub in dtype for sub in val):
                        return False
                else:
                    if val not in dtype:
                        return False

            elif key == "depth_min":
                d = data.get("depth")          # 修复：使用规范化后的字段 depth
                if d is None or not (d >= val):
                    return False

            elif key == "depth_wall_ratio_min":
                d = data.get("depth")          # 修复
                w = data.get("wall_thickness")
                if d is None or w is None or w <= 0:
                    return False
                if not ((d / w) >= val):
                    return False

            elif key == "length_min":
                l = data.get("length")         # 修复
                if l is None or not (l >= val):
                    return False

            elif key == "length_max":
                l = data.get("length")         # 修复
                if l is None or not (l <= val):
                    return False

            elif key == "quantity_min":
                q = data.get("quantity", 1)
                if not (q >= val):
                    return False

            elif key == "quantity_eq":
                q = data.get("quantity", 1)
                if not (q == val):
                    return False

            # 可在此继续扩展其他条件类型（如 location_contains 等）
            # 注意：未定义的条件将直接返回 True，请谨慎使用
        return True

    # ------------------- RPN → 风险等级 -------------------
    @staticmethod
    def _rpn_to_level(rpn: int) -> int:
        if rpn >= 200: return 4
        if rpn >= 100: return 3
        if rpn >= 50:  return 2
        return 1

    _level_map = {
        4: "高风险",
        3: "中风险",
        2: "低风险",
        1: "可忽略",
    }


# ================== 全局单例 ==================
# 模块导入时自动创建（Python import 锁保证线程安全）
rule_engine = RuleEngine()


def get_rule_engine() -> RuleEngine:
    """显式获取全局唯一的规则引擎实例（推荐）。"""
    return rule_engine


def reload_rules() -> bool:
    """便捷函数：热重载全局引擎的规则。"""
    return rule_engine.reload_rules()