# rule_engine.py
import json
from typing import List, Dict, Any

class RuleEngine:
    def __init__(self, rules_path: str = "rules.json"):
        with open(rules_path, 'r', encoding='utf-8') as f:
            self.rules = json.load(f)
        self.rules.sort(key=lambda r: r.get("priority", 0))  # 如果有优先级设置

    def evaluate(self, defect: Dict[str, Any]) -> Dict[str, Any]:
        """
        输入一条缺陷（包含 type, dimensions, wall_thickness 等），
        返回最终的 S/O/D/RPN/level 以及触发规则列表
        """
        # 初始化默认值（以防没有任何规则触发，可设一个安全兜底）
        s, o, d = 1, 1, 1
        triggered = []

        # 从缺陷数据中提取计算所需的值
        depth = defect.get("dimensions", {}).get("depth")
        length = defect.get("dimensions", {}).get("length")
        wall = defect.get("wall_thickness")
        # 如有剩余壁厚也可加入（通过 original_text 传到 defect 里）

        # 遍历所有规则
        for rule in self.rules:
            if self._match(rule["condition"], defect, depth, wall):
                triggered.append(rule)
                eff = rule["effect"]
                # 处理 severity
                if "severity" in eff:
                    s = eff["severity"]
                if "severity_delta" in eff:
                    s += eff["severity_delta"]
                # 处理 occurrence
                if "occurrence" in eff:
                    o = eff["occurrence"]
                if "occurrence_delta" in eff:
                    o += eff["occurrence_delta"]
                # 处理 detection
                if "detection" in eff:
                    d = eff["detection"]
                if "detection_delta" in eff:
                    d += eff["detection_delta"]

        # 边界限制（SOD 通常在1~10之间）
        s = max(1, min(10, s))
        o = max(1, min(10, o))
        d = max(1, min(10, d))
        rpn = s * o * d
        level = self._rpn_to_level(rpn)

        return {
            "severity": s,
            "occurrence": o,
            "detection": d,
            "rpn": rpn,
            "risk_level": self._level_map[level],
            "level": level,
            "triggered_rules": [r["rule_id"] for r in triggered],
            "standard_ref": ", ".join(set(r["source"] for r in triggered))
        }

    def _match(self, condition: dict, defect: dict, depth: float, wall: float) -> bool:
        # 检查缺陷类型
        if "type" in condition:
            if condition["type"] != defect.get("type"):
                return False
        # 检查深度/壁厚比例
        if "depth_wall_ratio_min" in condition and depth and wall:
            if depth / wall < condition["depth_wall_ratio_min"]:
                return False
        # 检查剩余壁厚比例（需要从 defect 中传入 remaining_wall 或计算）
        if "wall_remaining_ratio_max" in condition:
            # 假设 defect 有 remaining_wall 字段，或者从 original_text 中解析
            # 这里仅为示例，实际根据你可获得的数据调整
            remaining = defect.get("remaining_wall")
            if remaining is None or wall is None:
                return False
            if remaining / wall > condition["wall_remaining_ratio_max"]:
                return False
        # 可继续扩展长度、数量等条件...
        return True

    def _rpn_to_level(self, rpn: int) -> int:
        if rpn >= 200: return 4
        if rpn >= 100: return 3
        if rpn >= 50: return 2
        return 1

    _level_map = {4: "极高风险", 3: "高风险", 2: "低风险", 1: "可忽略"}

# 实例化引擎（整个项目共用一个实例即可）
rule_engine = RuleEngine()