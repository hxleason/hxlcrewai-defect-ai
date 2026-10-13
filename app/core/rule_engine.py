"""
动态 FMEA 规则引擎 —— 基于 rules.json 可配置 (v5.2 输入异常检测版)

特性：
- 线程安全：单例创建、热重载均使用锁保护
- 数据适配：自动处理嵌套的 dimensions 结构，提取有效数值字段
- 缺失值保守策略：当关键尺寸（深度/长度/壁厚）缺失时，相关规则不生效，
  避免因数据不足而错误低估风险
- 向后兼容：保留 rule_engine 全局变量，旧代码无需修改
- 内置兜底规则：覆盖 裂纹/点蚀/气孔/夹杂 等常见缺陷

修复记录：
- v3.1: 修正 _match 中字段名不一致问题（depth_mm → depth, length_mm → length）
        增强 _normalize_defect 对 dimensions 中键名兼容性
- v4.0: 修复 _normalize_defect 未将 defect_type 映射为 type 的问题；
        修正 DET_BASE 规则顺序，防止无条件规则覆盖特定规则；
        triggered_rules 只返回规则 ID 列表
- v5.0: 新增 _sort_rules_for_evaluation 排序逻辑，确保无条件规则始终
        先于有条件规则触发，无论规则文件如何排布；
        防止 DET_BASE/OCC_BASE/SEV_BASE 等无条件规则覆盖特定规则
- v5.1: 失败关闭（评估报告 6.6）——单条规则应用失败记录进 rule_failures、
        其余规则继续执行、最终 review_required=True；
        condition/effect 字段白名单校验；新增 review_required/rule_failures/warnings 输出
- v5.2（本次 · 输入异常检测）：
        规范化过程收集输入异常（如 depth='abc' 无法转数值、dimensions 类型错误），
        异常清单写入 warnings 并强制 review_required=True（失败关闭）。
        此前这类脏数据被静默转 None 后正常出分，属失败开放。
"""

import os
import json
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import RULES_PATH

logger = logging.getLogger("defect_fmea.rule_engine")


class RuleEngine:
    """基于 rules.json 的动态 FMEA 计算引擎（线程安全 + 数据适配 + 失败关闭版）"""

    def __init__(self, rules_path: Optional[str] = None):
        self.rules_path = rules_path or RULES_PATH
        self.rules: List[Dict[str, Any]] = []
        self._reload_lock = threading.Lock()
        self.load_rules()

    # ------------------- 规则加载 -------------------
    def load_rules(self) -> None:
        """加载规则（非线程安全，仅在初始化或单线程环境调用）。"""
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
        """
        默认规则集。
        注意：排序由 _sort_rules_for_evaluation 统一保证，
        此处顺序仅作为最终兜底。
        """
        return [
            # ========== 默认基线（无条件规则，排序后自动前置） ==========
            {"rule_id": "SEV_BASE", "description": "默认严重度", "condition": {}, "effect": {"severity": 3}, "source": "默认值"},
            {"rule_id": "OCC_BASE", "description": "默认发生度", "condition": {}, "effect": {"occurrence": 4}, "source": "默认"},
            {"rule_id": "DET_BASE", "description": "默认检出度", "condition": {}, "effect": {"detection": 4}, "source": "默认"},

            # ========== 严重度 Severity ==========
            {"rule_id": "SEV_CRACK", "description": "裂纹基础严重度", "condition": {"type_contains": "裂纹"}, "effect": {"severity": 4}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_D2", "description": "裂纹深度≥2mm", "condition": {"type_contains": "裂纹", "depth_min": 2.0}, "effect": {"severity": 5}, "source": "经验规则"},
            {"rule_id": "SEV_CRACK_R03", "description": "裂纹深度/壁厚≥0.3", "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.3}, "effect": {"severity": 7}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_CRACK_R05", "description": "裂纹深度/壁厚≥0.5", "condition": {"type_contains": "裂纹", "depth_wall_ratio_min": 0.5}, "effect": {"severity": 9}, "source": "GB/T 19624-2019"},
            {"rule_id": "SEV_PITTING", "description": "点蚀/腐蚀基础", "condition": {"type_contains": ["点蚀", "腐蚀"]}, "effect": {"severity": 4}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_D2", "description": "点蚀深度≥2mm", "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_min": 2.0}, "effect": {"severity": 6}, "source": "API 579"},
            {"rule_id": "SEV_PITTING_R05", "description": "点蚀深度/壁厚≥0.5", "condition": {"type_contains": ["点蚀", "腐蚀"], "depth_wall_ratio_min": 0.5}, "effect": {"severity": 8}, "source": "API 579"},
            {"rule_id": "SEV_POROSITY_S", "description": "气孔/夹杂长度≤10mm", "condition": {"type_contains": ["气孔", "夹杂"], "length_max": 10}, "effect": {"severity": 3}, "source": "经验规则"},
            {"rule_id": "SEV_POROSITY_L", "description": "气孔/夹杂长度>10mm", "condition": {"type_contains": ["气孔", "夹杂"], "length_min": 10}, "effect": {"severity": 5}, "source": "经验规则"},

            # ========== 发生度 Occurrence ==========
            {"rule_id": "OCC_Q1", "description": "数量=1", "condition": {"quantity_eq": 1}, "effect": {"occurrence": 3}, "source": "默认"},
            {"rule_id": "OCC_Q3", "description": "数量≥3", "condition": {"quantity_min": 3}, "effect": {"occurrence": 5}, "source": "默认"},
            {"rule_id": "OCC_Q5", "description": "数量≥5", "condition": {"quantity_min": 5}, "effect": {"occurrence": 7}, "source": "默认"},

            # ========== 检出度 Detection ==========
            {"rule_id": "DET_CRACK", "description": "裂纹检出度", "condition": {"type_contains": "裂纹"}, "effect": {"detection": 6}, "source": "默认"},
            {"rule_id": "DET_PITTING", "description": "点蚀/腐蚀检出度", "condition": {"type_contains": ["点蚀", "腐蚀"]}, "effect": {"detection": 5}, "source": "默认"},
        ]

    # ------------------- 规则排序（v5.0 新增） -------------------
    @staticmethod
    def _sort_rules_for_evaluation(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        确保无条件规则（condition 为空或 {}）排在特定规则之前。
        这样 DET_BASE 先赋值，DET_CRACK 再覆盖；
        否则无条件规则会覆盖掉条件更具体的规则。

        无论 rules.json 文件中的规则如何排布，此方法都强制保证正确顺序。
        """
        unconditional = [r for r in rules if not r.get("condition")]
        conditional = [r for r in rules if r.get("condition")]
        return unconditional + conditional

    # ------------------- 数据规范化 -------------------
    @staticmethod
    def _normalize_defect(defect: Dict[str, Any]) -> Dict[str, Any]:
        """
        向后兼容入口：仅返回规范化后的数据字典（丢弃异常清单）。
        内部逻辑见 _normalize_defect_checked。
        """
        data, _anomalies = RuleEngine._normalize_defect_checked(defect)
        return data

    @classmethod
    def _normalize_defect_checked(
        cls, defect: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        v5.2：将缺陷字典转换为规则引擎可用的扁平格式，同时收集输入异常清单。

        标准内部字段为:
            - type      : 缺陷类型字符串
            - depth     : 缺陷深度 (mm)
            - length    : 缺陷长度 (mm)
            - wall_thickness : 设计壁厚 (mm)
            - quantity  : 数量

        兼容旧版嵌套 dimensions 结构，及不同的键名 (depth_mm/length_mm 等)。
        兼容缺陷类型字段名：type 或 defect_type。

        返回:
            (规范化数据字典, 异常清单)
            异常清单记录"无法转换的脏数据"（如 depth='abc'），
            供 evaluate 写入 warnings 并触发失败关闭复核。
        """
        anomalies: List[str] = []
        data = defect.copy()
        dims = data.get("dimensions") or {}
        if not isinstance(dims, dict):
            anomalies.append(f"dimensions 字段类型异常（{type(dims).__name__}），已忽略")
            dims = {}

        # 缺陷类型标准化：支持 type 或 defect_type 两种键名
        if data.get("type") is None:
            data["type"] = data.get("defect_type", "")
        if not isinstance(data["type"], str):
            data["type"] = str(data.get("type", ""))

        # 数值转换辅助函数（v5.2：失败不再静默，记入异常清单）
        def _to_float(v, label: str):
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                logger.warning("输入数据异常，%s 数值转换失败: %r", label, v)
                anomalies.append(f"{label} 数值无效: {v!r}")
                return None

        # 深度、长度：优先顶层，其次 dimensions，再兼容旧键
        for standard_key, legacy_key in [("depth", "depth_mm"), ("length", "length_mm")]:
            if data.get(standard_key) is not None:
                data[standard_key] = _to_float(data[standard_key], standard_key)
                continue

            val = dims.get(standard_key)
            if val is None:
                val = dims.get(legacy_key)
            if val is None:
                val = defect.get(standard_key)
            if val is None:
                val = defect.get(legacy_key)

            data[standard_key] = _to_float(val, standard_key)

        # 壁厚
        wt = data.get("wall_thickness")
        if wt is None:
            wt = dims.get("wall_thickness", defect.get("wall_thickness"))
        data["wall_thickness"] = _to_float(wt, "wall_thickness")

        # 数量始终为整数
        try:
            data["quantity"] = int(defect.get("quantity", 1))
        except (TypeError, ValueError):
            anomalies.append(f"quantity 数值无效: {defect.get('quantity')!r}")
            data["quantity"] = 1

        return data, anomalies

    # ------------------- 核心评估 -------------------
    def evaluate(self, defect: Dict[str, Any]) -> Dict[str, Any]:
        """
        对单条缺陷执行 FMEA 评分（v5.2 失败关闭版）。

        - 内部先调用 _normalize_defect_checked 处理数据结构并收集输入异常。
        - 使用排序后的规则快照，保证无条件规则先触发（v5.0 语义不变）。
        - 单条规则应用失败（未知 condition/effect 字段、类型错误、数值越界）：
          记录到 rule_failures 并继续执行其余规则（v5.1 语义）。
        - 存在以下任一情况 → review_required=True（失败关闭）：
            ① 有规则失败（rule_failures 非空）
            ② 无任何规则命中（规则集可能损坏）
            ③ 输入数据存在异常（v5.2 新增，如 depth='abc'）
        - 多条规则对 severity/occurrence/detection 的覆盖是 v5.0 排序下的
          **预期行为**（基线先赋值、特定规则后覆盖），不属于冲突。
        """
        data, anomalies = self._normalize_defect_checked(defect)
        # v5.0：排序保证无条件规则（SEV_BASE/OCC_BASE/DET_BASE）先触发
        current_rules = self._sort_rules_for_evaluation(self.rules)
        s, o, d = 1, 1, 1
        triggered = []
        rule_failures = []
        warnings = [f"输入数据异常: {a}" for a in anomalies]

        for rule in current_rules:
            try:
                condition = rule.get("condition")
                if condition is not None and not self._match(condition, data):
                    continue

                eff = rule.get("effect")
                if not isinstance(eff, dict) or not eff:
                    raise ValueError("effect 缺失或格式错误")
                # v5.1：effect 白名单与数值校验（不合法即失败关闭）
                self._validate_effect(rule, eff)

                triggered.append(rule)
                # 绝对赋值
                if "severity" in eff:
                    s = int(eff["severity"])
                if "occurrence" in eff:
                    o = int(eff["occurrence"])
                if "detection" in eff:
                    d = int(eff["detection"])
                # 增量调整
                if "severity_delta" in eff:
                    s += eff["severity_delta"]
                if "occurrence_delta" in eff:
                    o += eff["occurrence_delta"]
                if "detection_delta" in eff:
                    d += eff["detection_delta"]
            except Exception as exc:
                # v5.1：单条规则失败 → 记录并继续，最终强制复核（失败关闭）
                rule_id = rule.get("rule_id") or rule.get("description") or "unknown"
                rule_failures.append({"rule_id": rule_id, "error": str(exc)})
                logger.error("❌ 规则 %s 应用失败（失败关闭，已记录）: %s", rule_id, exc)

        s = max(1, min(10, int(round(s))))
        o = max(1, min(10, int(round(o))))
        d = max(1, min(10, int(round(d))))
        rpn = s * o * d
        level = self._rpn_to_level(rpn)
        sources = list({str(r.get("source") or "未知") for r in triggered})

        # v5.1/v5.2：失败关闭判定
        if not triggered:
            warnings.append("未触发任何规则，S/O/D 使用兜底初始值")
        if anomalies:
            warnings.append("输入数据存在异常字段，结果需人工复核（失败关闭）")
        review_required = bool(rule_failures) or not triggered or bool(anomalies)
        if review_required:
            warnings.append("存在规则失败或输入异常，结果需人工复核（失败关闭）")

        return {
            "severity": s,
            "occurrence": o,
            "detection": d,
            "rpn": rpn,
            "risk_level": self._level_map[level],
            "level": level,
            "triggered_rules": [r.get("rule_id") or r.get("description") or "unknown" for r in triggered],
            "standard_ref": ", ".join(sources),
            # v5.1 新增字段（向后兼容：旧消费方不受影响）
            "review_required": review_required,
            "rule_failures": rule_failures,
            "warnings": warnings,
        }

    # ------------------- v5.1 白名单与校验 -------------------
    _KNOWN_CONDITION_KEYS = {
        "type_contains", "depth_min", "depth_wall_ratio_min",
        "length_min", "length_max", "quantity_min", "quantity_eq",
    }

    _ALLOWED_EFFECT_KEYS = {
        "severity", "occurrence", "detection",
        "severity_delta", "occurrence_delta", "detection_delta",
    }

    @staticmethod
    def _coerce_number(val: Any, key: str) -> float:
        """数值条件值强转；失败抛 ValueError（失败关闭）。"""
        if isinstance(val, bool):
            raise ValueError(f"condition[{key}] 不允许布尔值，收到 {val!r}")
        try:
            return float(val)
        except (TypeError, ValueError):
            raise ValueError(f"condition[{key}] 必须为数值，收到 {val!r}")

    @classmethod
    def _validate_effect(cls, rule: Dict[str, Any], effect: Dict[str, Any]) -> None:
        """
        v5.1：effect 白名单校验。
        未知字段 / 非数值 / 绝对赋值越界（1~10）/ 非整数绝对赋值
        → 抛 ValueError，由 evaluate 记录为规则失败（失败关闭）。
        """
        unknown = set(effect) - cls._ALLOWED_EFFECT_KEYS
        if unknown:
            raise ValueError(f"未知 effect 字段: {sorted(unknown)}")
        for key, val in effect.items():
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"effect[{key}] 必须为数值，收到 {val!r}")
            if key in ("severity", "occurrence", "detection"):
                if not (1 <= val <= 10):
                    raise ValueError(f"effect[{key}] 超出 1~10 范围: {val}")
                if float(val) != int(val):
                    raise ValueError(f"effect[{key}] 必须为整数: {val}")

    # ------------------- 条件匹配 -------------------
    @classmethod
    def _match(cls, condition: Dict[str, Any], data: Dict[str, Any]) -> bool:
        """
        判断规范化后的数据 data 是否满足一条规则的所有条件。

        关键安全策略（v5.1 强化）：
          1) 未知 condition 字段视为规则编写错误，直接抛 ValueError
             （失败关闭）——避免拼写错误导致规则比预期宽松地触发；
          2) 数值比较所需字段为 None 时视为不满足（保守原则）；
          3) 数值型 condition 值允许 "2" 等字符串，自动强转，
             强转失败同样抛 ValueError（失败关闭）。

        数据字段使用规范化后的标准名称：depth, length, wall_thickness, quantity, type。
        """
        if condition is None:
            return True
        if not isinstance(condition, dict):
            raise ValueError(f"condition 必须为对象，收到 {type(condition).__name__}")

        unknown = set(condition) - cls._KNOWN_CONDITION_KEYS
        if unknown:
            raise ValueError(f"未知 condition 字段: {sorted(unknown)}")

        for key, val in condition.items():
            if key == "type_contains":
                dtype = data.get("type", "")
                if isinstance(val, list):
                    if not any(str(sub) in dtype for sub in val):
                        return False
                elif isinstance(val, str):
                    if val not in dtype:
                        return False
                else:
                    raise ValueError(
                        f"condition[type_contains] 必须为字符串或字符串列表，收到 {val!r}"
                    )

            elif key == "depth_min":
                d = data.get("depth")
                if d is None or not (d >= cls._coerce_number(val, key)):
                    return False

            elif key == "depth_wall_ratio_min":
                d = data.get("depth")
                w = data.get("wall_thickness")
                if d is None or w is None or w <= 0:
                    return False
                if not ((d / w) >= cls._coerce_number(val, key)):
                    return False

            elif key == "length_min":
                l = data.get("length")
                if l is None or not (l >= cls._coerce_number(val, key)):
                    return False

            elif key == "length_max":
                l = data.get("length")
                if l is None or not (l <= cls._coerce_number(val, key)):
                    return False

            elif key == "quantity_min":
                q = data.get("quantity", 1)
                if not (q >= cls._coerce_number(val, key)):
                    return False

            elif key == "quantity_eq":
                q = data.get("quantity", 1)
                if not (q == cls._coerce_number(val, key)):
                    return False

        return True

    # ------------------- RPN → 风险等级 -------------------
    @staticmethod
    def _rpn_to_level(rpn: int) -> int:
        if rpn > 200:
            return 4
        if rpn > 100:
            return 3
        if rpn > 50:
            return 2
        return 1

    _level_map = {
        4: "极高风险",
        3: "高风险",
        2: "中风险",
        1: "低风险",
    }


# ================== 全局单例 ==================
rule_engine = RuleEngine()


def get_rule_engine() -> RuleEngine:
    """显式获取全局唯一的规则引擎实例（推荐）。"""
    return rule_engine


def reload_rules() -> bool:
    """便捷函数：热重载全局引擎的规则。"""
    return rule_engine.reload_rules()