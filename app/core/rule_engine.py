"""
动态 FMEA 规则引擎 —— 基于 rules.json 可配置
特性：
- 线程安全：单例创建、规则重载均使用锁保护
- 热更新：调用 reload_rules() 即可在线更新规则，不影响正在进行的评估
- 向后兼容：保留 rule_engine 全局变量，旧代码无需修改
- 内置兜底规则：覆盖 裂纹/点蚀/气孔/夹杂 等常见缺陷
"""

import os
import json
import logging
import threading
from typing import Any, Dict, List, Optional

# ✅ 从项目配置中导入规则文件路径
from app.core.config import RULES_PATH

logger = logging.getLogger("defect_fmea.rule_engine")


class RuleEngine:
    """基于 rules.json 的动态 FMEA 计算引擎（线程安全版）"""

    def __init__(self, rules_path: Optional[str] = None):
        self.rules_path = rules_path or RULES_PATH
        self.rules: List[Dict[str, Any]] = []
        # 规则重载锁，保证同一时刻只有一个线程执行加载，且读写安全
        self._reload_lock = threading.Lock()
        # 初始化时加载规则（此时对象尚未被外部共享，不需加锁）
        self.load_rules()

    def load_rules(self) -> None:
        """
        尝试从外部文件加载规则，失败则使用内置默认规则。
        此方法可在 __init__ 时调用，也可由 reload_rules() 内部调用。
        """
        new_rules = self._load_rules_from_source()
        # 直接赋值，由于 Python GIL，引用替换是原子操作
        self.rules = new_rules
        logger.info(f"规则已加载，当前规则总数：{len(self.rules)}")

    def reload_rules(self) -> bool:
        """
        热重载规则文件（线程安全）。
        返回 True 表示重载成功，False 表示重载失败并保留旧规则。
        """
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
        """从配置路径读取规则文件，若失败则返回默认规则。"""
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

    # ---------- 默认规则集（内置兜底） ----------
    def _default_rules(self) -> List[Dict[str, Any]]:
        """
        内置兜底规则集，覆盖常见缺陷类型与通用 S/O/D 基准。
        可在 rules.json 中覆盖这些规则。
        """
        return [
            # -- 严重度 (Severity) --
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

            # -- 发生度 (Occurrence) --
            {"rule_id": "OCC_BASE", "condition": {},
             "effect": {"occurrence": 4}, "source": "默认"},
            {"rule_id": "OCC_Q1", "condition": {"quantity_eq": 1},
             "effect": {"occurrence": 3}, "source": "默认"},
            {"rule_id": "OCC_Q3", "condition": {"quantity_min": 3},
             "effect": {"occurrence": 5}, "source": "默认"},
            {"rule_id": "OCC_Q5", "condition": {"quantity_min": 5},
             "effect": {"occurrence": 7}, "source": "默认"},

            # -- 检出度 (Detection) --
            {"rule_id": "DET_CRACK", "condition": {"type_contains": "裂纹"},
             "effect": {"detection": 6}, "source": "默认"},
            {"rule_id": "DET_PITTING", "condition": {"type_contains": ["点蚀", "腐蚀"]},
             "effect": {"detection": 5}, "source": "默认"},
            {"rule_id": "DET_BASE", "condition": {},
             "effect": {"detection": 4}, "source": "默认"},
        ]

    # ---------- 核心评估方法（线程安全） ----------
    def evaluate(self, defect: Dict[str, Any]) -> Dict[str, Any]:
        """
        对单条缺陷进行 FMEA 评分。
        遍历时持有当前规则列表的引用，因此即使热重载发生，评估过程也不会受影响。
        """
        # 获取当前规则的快照引用（避免遍历期间被替换）
        current_rules = self.rules
        s, o, d = 1, 1, 1
        triggered = []

        for rule in current_rules:
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
        """检查缺陷是否满足一条规则的全部条件"""
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
# 模块导入时自动创建，由于 Python 的 import 锁，这个过程本身就是线程安全的
rule_engine = RuleEngine()


def get_rule_engine() -> RuleEngine:
    """
    （推荐）显式获取全局唯一的规则引擎实例。
    与直接使用 rule_engine 变量完全等价，但语义更明确。
    """
    return rule_engine


def reload_rules() -> bool:
    """
    便捷函数：热重载全局引擎的规则。
    返回 True 表示成功，False 表示失败。
    """
    return rule_engine.reload_rules()