"""
动态 FMEA 规则引擎 —— 基于 rules.json 可配置
若无规则文件，自动启用内置兜底规则（覆盖 裂纹/点蚀/气孔/夹杂 等常见缺陷）
"""

import os
import json
import logging
from typing import Any, Dict, List

# ✅ 关键修正：从 app.core.config 导入 RULES_PATH
from app.core.config import RULES_PATH

logger = logging.getLogger("defect_fmea.rule_engine")


class RuleEngine:
    """基于 rules.json 的动态 FMEA 计算引擎"""

    def __init__(self, rules_path: str = None):
        # 优先使用传入路径，否则使用全局配置的 RULES_PATH
        self.rules_path = rules_path or RULES_PATH
        self.rules: List[Dict[str, Any]] = []
        self.load_rules()

    def load_rules(self) -> None:
        """尝试加载外部规则文件，失败则使用内置规则"""
        if self.rules_path and os.path.exists(self.rules_path):
            try:
                with open(self.rules_path, "r", encoding="utf-8") as f:
                    self.rules = json.load(f)
                if not isinstance(self.rules, list):
                    raise ValueError("规则文件内容应为 JSON 数组")
                logger.info(
                    f"✅ 成功加载 {len(self.rules)} 条 FMEA 规则：{self.rules_path}"
                )
            except Exception as e:
                logger.error(f"规则文件加载失败：{e}，降级为内置规则")
                self.rules = self._default_rules()
        else:
            logger.warning(
                f"规则文件 {self.rules_path} 未找到，将使用内置兜底规则"
            )
            self.rules = self._default_rules()

    def _default_rules(self) -> List[Dict[str, Any]]:
        """
        内置兜底规则集（覆盖常见缺陷 + 通用 S/O/D 基准）
        你可以在 rules.json 中覆盖这些规则，或在运行时动态添加
        """
        return [
            # ---------- 严重度 (Severity) ----------
            {"rule_id": "SEV_BASE", "description": "默认严重度",
             "condition": {}, "effect": {"severity": 3}, "source": "默认值"},
            {"rule_id": "SEV_CRACK", "description": "裂纹基础严重度",
             "condition": {"type_contains": "裂纹"},
             "effect": {"severity": 4}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_D2", "description": "裂纹深度≥2mm",
             "condition": {"type_contains": "裂纹", "depth_min": 2.0},
             "effect": {"severity": 5}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_R03", "description": "裂纹深度/壁厚≥0.3",
             "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.3},
             "effect": {"severity": 7}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_CRACK_R05", "description": "裂纹深度/壁厚≥0.5",
             "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.5},
             "effect": {"severity": 9}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_PITTING", "description": "点蚀/腐蚀基础",
             "condition": {"type_contains": ["点蚀", "腐蚀"]},
             "effect": {"severity": 4}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_D2", "description": "点蚀深度≥2mm",
             "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_min": 2.0},
             "effect": {"severity": 6}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_R05", "description": "点蚀深度/壁厚≥0.5",
             "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_wall_ratio_min": 0.5},
             "effect": {"severity": 8}, "source": "API 579"},
            {"rule_id": "SEV_POROSITY_S", "description": "气孔/夹杂长度≤10mm",
             "condition": {"type_contains": ["气孔", "夹杂"], "length_max": 10},
             "effect": {"severity": 3}, "source": "经验规则"},
            {"rule_id": "SEV_POROSITY_L", "description": "气孔/夹杂长度>10mm",
             "condition": {"type_contains": ["气孔", "夹杂"], "length_min": 10},
             "effect": {"severity": 5}, "source": "经验规则"},

            # ---------- 发生度 (Occurrence) ----------
            {"rule_id": "OCC_BASE", "condition": {},
             "effect": {"occurrence": 4}, "source": "默认"},
            {"rule_id": "OCC_Q1", "condition": {"quantity_eq": 1},
             "effect": {"occurrence": 3}, "source": "默认"},
            {"rule_id": "OCC_Q3", "condition": {"quantity_min": 3},
             "effect": {"occurrence": 5}, "source": "默认"},
            {"rule_id": "OCC_Q5", "condition": {"quantity_min": 5},
             "effect": {"occurrence": 7}, "source": "默认"},

            # ---------- 检出度 (Detection) ----------
            {"rule_id": "DET_CRACK", "condition": {"type_contains": "裂纹"},
             "effect": {"detection": 6}, "source": "默认"},
            {"rule_id": "DET_PITTING", "condition": {"type_contains": ["点蚀", "腐蚀"]},
             "effect": {"detection": 5}, "source": "默认"},
            {"rule_id": "DET_BASE", "condition": {},
             "effect": {"detection": 4}, "source": "默认"},
        ]

    # ---------- 规则评估 ----------
    def evaluate(self, defect: Dict[str, Any]) -> Dict[str, Any]:
        """
        对一条缺陷进行 FMEA 评分
        :param defect: 包含 type, length_mm, depth_mm, wall_thickness, quantity 等字段
        :return: severity, occurrence, detection, rpn, risk_level 等
        """
        # 初始值（安全值）
        s, o, d = 1, 1, 1
        triggered = []

        for rule in self.rules:
            if self._match(rule.get("condition", {}), defect):
                triggered.append(rule)
                eff = rule.get("effect", {})
                if "severity" in eff:
                    s = eff["severity"]
                if "severity_delta" in eff:
                    s += eff["severity_delta"]
                if "occurrence" in eff:
                    o = eff["occurrence"]
                if "occurrence_delta" in eff:
                    o += eff["occurrence_delta"]
                if "detection" in eff:
                    d = eff["detection"]
                if "detection_delta" in eff:
                    d += eff["detection_delta"]

        # 限制在 1-10 之间
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

    # ---------- 条件匹配逻辑 ----------
    def _match(self, condition: Dict[str, Any], defect: Dict[str, Any]) -> bool:
        """
        逐条检查 condition 中的每个键值对，只有全部满足才返回 True
        """
        for key, val in condition.items():
            if key == "type_contains":
                dtype = defect.get("type", "")
                if isinstance(val, list):
                    if not any(sub in dtype for sub in val):
                        return False
                else:
                    if val not in dtype:
                        return False
            elif key == "depth_min":
                depth = defect.get("depth_mm", 0)
                if not (depth >= val):
                    return False
            elif key == "depth_wall_ratio_min":
                depth = defect.get("depth_mm")
                wall = defect.get("wall_thickness")
                if not (depth and wall and wall > 0 and (depth / wall) >= val):
                    return False
            elif key == "length_min":
                length = defect.get("length_mm", 0)
                if not (length >= val):
                    return False
            elif key == "length_max":
                length = defect.get("length_mm", 0)
                if not (length <= val):
                    return False
            elif key == "quantity_min":
                qty = defect.get("quantity", 1)
                if not (qty >= val):
                    return False
            elif key == "quantity_eq":
                qty = defect.get("quantity", 1)
                if not (qty == val):
                    return False
        return True

    # ---------- RPN → 风险等级 ----------
    @staticmethod
    def _rpn_to_level(rpn: int) -> int:
        if rpn >= 200: return 4   # 高风险
        if rpn >= 100: return 3   # 中风险
        if rpn >= 50:  return 2   # 低风险
        return 1                   # 可忽略

    _level_map = {
        4: "高风险",
        3: "中风险",
        2: "低风险",
        1: "可忽略",
    }


# ================== 全局单例 ==================
rule_engine = RuleEngine()