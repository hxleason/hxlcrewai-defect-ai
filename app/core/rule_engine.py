import os
import json
import logging
from typing import Any, Dict, List
from app.config import RULES_PATH

logger = logging.getLogger("defect_fmea.rule_engine")

class RuleEngine:
    """基于 rules.json 的动态 FMEA 计算引擎"""

    def __init__(self, rules_path: str = None):
        self.rules_path = rules_path or RULES_PATH
        self.rules: List[Dict[str, Any]] = []
        self.load_rules()

    def load_rules(self):
        if os.path.exists(self.rules_path):
            with open(self.rules_path, "r", encoding="utf-8") as f:
                self.rules = json.load(f)
            logger.info(f"成功加载 {len(self.rules)} 条 FMEA 规则：{self.rules_path}")
        else:
            logger.warning(f"规则文件 {self.rules_path} 未找到，将使用内置兜底逻辑")
            self.rules = self._default_rules()

    def _default_rules(self) -> List[Dict[str, Any]]:
        return [
            {"rule_id": "SEV_BASE", "description": "默认严重度", "condition": {},
             "effect": {"severity": 3}, "source": "默认值"},
            {"rule_id": "SEV_CRACK", "condition": {"type_contains": "裂纹"},
             "effect": {"severity": 4}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_D2", "condition": {"type_contains": "裂纹", "depth_min": 2.0},
             "effect": {"severity": 5}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_R03", "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.3},
             "effect": {"severity": 7}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_CRACK_R05", "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.5},
             "effect": {"severity": 9}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_PITTING", "condition": {"type_contains": ["点蚀", "腐蚀"]},
             "effect": {"severity": 4}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_D2", "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_min": 2.0},
             "effect": {"severity": 6}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_R05", "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_wall_ratio_min": 0.5},
             "effect": {"severity": 8}, "source": "API 579"},
            {"rule_id": "SEV_POROSITY_S", "condition": {"type_contains": ["气孔", "夹杂"], "length_max": 10},
             "effect": {"severity": 3}, "source": "经验规则"},
            {"rule_id": "SEV_POROSITY_L", "condition": {"type_contains": ["气孔", "夹杂"], "length_min": 10},
             "effect": {"severity": 5}, "source": "经验规则"},
            {"rule_id": "OCC_BASE", "condition": {}, "effect": {"occurrence": 4}, "source": "默认"},
            {"rule_id": "OCC_Q1", "condition": {"quantity_eq": 1}, "effect": {"occurrence": 3}, "source": "默认"},
            {"rule_id": "OCC_Q3", "condition": {"quantity_min": 3}, "effect": {"occurrence": 5}, "source": "默认"},
            {"rule_id": "OCC_Q5", "condition": {"quantity_min": 5}, "effect": {"occurrence": 7}, "source": "默认"},
            {"rule_id": "DET_CRACK", "condition": {"type_contains": "裂纹"}, "effect": {"detection": 6}, "source": "默认"},
            {"rule_id": "DET_PITTING", "condition": {"type_contains": ["点蚀", "腐蚀"]},
             "effect": {"detection": 5}, "source": "默认"},
            {"rule_id": "DET_BASE", "condition": {}, "effect": {"detection": 4}, "source": "默认"},
        ]

    def evaluate(self, defect: Dict[str, Any]) -> Dict[str, Any]:
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

    def _match(self, condition: Dict[str, Any], defect: Dict[str, Any]) -> bool:
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

    @staticmethod
    def _rpn_to_level(rpn: int) -> int:
        if rpn >= 200: return 4
        if rpn >= 100: return 3
        if rpn >= 50:  return 2
        return 1

    _level_map = {4: "高风险", 3: "中风险", 2: "低风险", 1: "可忽略"}

# 全局单例
rule_engine = RuleEngine()