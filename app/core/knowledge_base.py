#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app/core/knowledge_base.py
知识库引擎模块（v3.6 · 终极版）

功能：
1. 加载并管理专家规则库和失效案例库；
2. 提供规则匹配、规则调整应用；
3. 提供相似案例检索和基线 S/O/D 提取；
4. 单例模式，全局唯一实例；
5. 支持路径配置和健壮的错误处理。

v3.6 修复与增强：
- 修正相似案例措施提取：统一从 case 的 "measures" 或 "corrective_measures" 字段提取，
  确保返回的 measures 始终为列表（可为空列表），避免下游获得空值。
- 优化措施分割：回退分割时仅按分号/换行，不按逗号，保持与构建脚本一致。
- 案例加载兼容多种 JSON 结构：{"cases": [...]}、{"case_list": [...]}、顶层列表。
- 相似度计算增强：device_type 同时匹配 equipment_category / equipment_subcategory；
  failure_mode 同时匹配 failure_mode / failure_phenomenon。
- 保留 v3.4 全部修复：自由文本严格匹配、AND/OR 复合逻辑等。
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------- 路径配置 ----------
try:
    from app.core.config import PROJECT_ROOT
except ImportError:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_RULES_PATH = PROJECT_ROOT / "data" / "expert_rules.json"
DEFAULT_CASES_PATH = PROJECT_ROOT / "data" / "failure_cases.json"

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    知识库管理类（单例）

    属性:
        rules: 专家规则列表（List[Dict]）
        cases: 失效案例列表（List[Dict]）
    """

    _instance = None  # 单例实例

    # ---------- 字段关键字映射（用于解析规则条件） ----------
    FIELD_KEYWORDS = {
        "type": ["缺陷类型", "类型"],
        "defect_type": ["缺陷类型", "类型"],
        "location": ["位置", "部位", "焊缝"],
        "media": ["介质", "充装介质"],
        "material": ["材质", "材料"],
        "device_type": ["设备类型", "设备", "容器"],
        "component": ["部件", "组件"],
        "environment": ["环境"],
    }

    # ---------- 字段别名映射 ----------
    RULE_FIELD_ALIASES = {
        "type": "defect_type",
        "defect_type": "type",
        "location": "component",
        "component": "location",
    }

    # ---------- 专家规则调整幅度硬上限 ----------
    DEFAULT_MAX_S_ADJ = 5
    DEFAULT_MAX_O_ADJ = 3
    DEFAULT_MAX_D_ADJ = 2   # v3.0: 由3降为2

    # ---------- 空条件规则是否触发 ----------
    ALLOW_EMPTY_CONDITION_MATCH = False

    # ---------- 数值型特征字段名（不参与文本匹配） ----------
    NUMERIC_FEATURE_KEYS = {
        "length_mm", "depth_mm", "wall_thickness", "quantity",
        "rpn", "severity", "occurrence", "detection",
    }

    # ---------- 缺陷类型关键词（用于强约束检测） ----------
    DEFECT_TYPE_KEYWORDS = [
        "缺陷类型", "类型", "裂纹", "腐蚀", "点蚀",
        "气孔", "夹杂", "磨损", "变形", "渗漏", "断裂",
    ]

    # ---------- 否定语境模式 ----------
    NEGATION_PATTERNS = [
        "不适用", "不包含", "排除", "除外", "非",
        "不用于", "不认为", "不属于", "不涉及", "剔除", "不匹配",
    ]

    # ---------- 自由文本子句阻止关键词 ----------
    # 当子句包含这些词时，说明带有额外约束，不能仅凭缺陷类型匹配
    FREETEXT_BLOCKLIST_PATTERN = re.compile(
        r"尺寸|深度|长度|范围|环境|湿度|漆层|介质|允许|打磨|开裂倾向|交变载荷|SCC|HIC|IGSCC|Cl⁻|腐蚀"
    )

    # ---------- 自由文本上下文停用词 ----------
    # 用于在自由文本子句移除缺陷类型后，过滤掉常见的虚词、连接词和泛化词
    FREETEXT_STOP_WORDS = {
        "存在", "有", "出现", "产生", "发生", "含有", "具有", "呈现", "导致",
        "引起", "造成", "为", "是", "的", "及", "和", "、", "或", "且", "并",
        "等", "缺陷", "检测", "发现", "出现", "存在", "表面", "内部", "区域",
        "部位", "处", "时", "中", "上", "下", "内", "外", "各", "该", "其",
    }

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        rules_path: Optional[Path] = None,
        cases_path: Optional[Path] = None,
        auto_load: bool = True,
    ):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self.rules_path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
        self.cases_path = Path(cases_path) if cases_path else DEFAULT_CASES_PATH
        self.rules: List[Dict[str, Any]] = []
        self.cases: List[Dict[str, Any]] = []

        self._initialized = True

        if auto_load:
            self.load_all()

    # ---------- 文件读取 ----------

    def _read_json(self, file_path: Path) -> Any:
        if not file_path.exists():
            raise FileNotFoundError(f"知识库文件不存在: {file_path}")

        with open(file_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)

    # ---------- 数据加载 ----------

    def load_rules(self) -> None:
        try:
            data = self._read_json(self.rules_path)
            if isinstance(data, dict):
                if "rules" in data and isinstance(data["rules"], list):
                    self.rules = data["rules"]
                else:
                    logger.warning("规则文件格式异常，期望顶层包含 'rules' 键，尝试将整个 dict 视为单条规则列表")
                    self.rules = [data]
            elif isinstance(data, list):
                self.rules = data
            else:
                raise ValueError("规则文件格式错误：顶层必须为 list 或包含 'rules' 键的 dict")

            logger.info(f"成功加载 {len(self.rules)} 条专家规则")
        except Exception as e:
            logger.error(f"加载规则库失败: {e}")
            raise

    def load_cases(self) -> None:
        try:
            data = self._read_json(self.cases_path)
            if isinstance(data, dict):
                # 兼容多种可能的键名
                for key in ("cases", "case_list", "data"):
                    if key in data and isinstance(data[key], list):
                        self.cases = data[key]
                        break
                else:
                    # 若没有标准键，则尝试将整个 dict 视为单条案例
                    logger.warning("案例文件格式异常，期望顶层包含 'cases' 键，尝试将整个 dict 视为单条案例列表")
                    self.cases = [data]
            elif isinstance(data, list):
                self.cases = data
            else:
                raise ValueError("案例文件格式错误：顶层必须为 list 或包含 'cases' 键的 dict")

            logger.info(f"成功加载 {len(self.cases)} 条失效案例")
        except Exception as e:
            logger.error(f"加载案例库失败: {e}")
            raise

    def load_all(self) -> None:
        self.load_rules()
        self.load_cases()

    def reload(self) -> None:
        self.rules.clear()
        self.cases.clear()
        self.load_all()

    # ---------- 基础查询方法 ----------

    def get_rule_by_id(self, rule_id: str) -> Optional[Dict[str, Any]]:
        for rule in self.rules:
            if rule.get("rule_id") == rule_id:
                return rule
        return None

    def get_rules_by_class(self, rule_class: str) -> List[Dict[str, Any]]:
        return [rule for rule in self.rules if rule.get("rule_class") == rule_class]

    def get_all_rule_classes(self) -> List[str]:
        seen = set()
        classes = []
        for rule in self.rules:
            rc = rule.get("rule_class")
            if rc and rc not in seen:
                seen.add(rc)
                classes.append(rc)
        return classes

    def get_case_by_id(self, case_id: str) -> Optional[Dict[str, Any]]:
        for case in self.cases:
            if case.get("case_id") == case_id:
                return case
        return None

    # ---------- 字符串相似度辅助 ----------

    @staticmethod
    def _has_common_substring(s1: str, s2: str, min_len: int = 2) -> bool:
        """
        判断两个字符串是否包含长度至少为 min_len 的公共子串（忽略大小写）
        """
        if not s1 or not s2:
            return False
        s1, s2 = s1.lower(), s2.lower()
        len1, len2 = len(s1), len(s2)
        if len1 < min_len or len2 < min_len:
            return s1 == s2
        for i in range(len1 - min_len + 1):
            sub = s1[i:i + min_len]
            if sub in s2:
                return True
        return False

    def _condition_negates_defect_type(self, if_condition: str, defect_type: str) -> bool:
        """
        v3.2 双向否定识别：
        在条件中搜索每个否定词，并检查否定词前后各 12 个字符的窗口内
        是否出现 defect_type 关键词。命中则返回 True（该类型被否定）。
        """
        if not defect_type or not if_condition:
            return False

        defect_lower = str(defect_type).lower()
        cond_lower = if_condition.lower()

        for pattern in self.NEGATION_PATTERNS:
            start = 0
            while True:
                idx = cond_lower.find(pattern.lower(), start)
                if idx == -1:
                    break

                before_start = max(0, idx - 12)
                after_end = min(len(cond_lower), idx + len(pattern) + 12)
                window = cond_lower[before_start:after_end]

                if defect_lower in window:
                    logger.debug(
                        "规则条件中检出否定缺陷类型：pattern='%s' window='%s'",
                        pattern, window,
                    )
                    return True

                start = idx + len(pattern)

        return False

    # ---------- 专家规则匹配 ----------

    def match_expert_rules(self, features: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        根据特征信息匹配专家规则。

        匹配结果按条件特异性（可解析条款数）降序排列，
        保证在 apply_rule_adjustments 中优先应用最具体的规则。
        """
        matched_rules = []
        for rule in self.rules:
            if_condition = rule.get("if_condition", "")
            if self._rule_matches_features(if_condition, features):
                matched_rules.append(rule)

        matched_rules.sort(
            key=lambda r: self._condition_specificity(r.get("if_condition", "")),
            reverse=True,
        )

        logger.debug(f"特征 {features} 匹配到 {len(matched_rules)} 条规则")
        return matched_rules

    # ---------- 条件解析辅助 ----------

    @staticmethod
    def _split_condition_clauses(condition: str) -> List[List[str]]:
        """
        将条件字符串按 OR/AND 解析为结构化条件组（v3.3）。

        返回格式：List[List[str]]
        - 外层列表：每个元素是一个 OR 组
        - 内层列表：每个元素是该 OR 组内的一个 AND 条件字符串
        """
        if not condition:
            return [[]]

        cleaned = condition.strip()
        # 去除可能的前缀 IF
        if re.match(r"^IF\s+", cleaned, flags=re.IGNORECASE):
            cleaned = cleaned[3:].strip()

        # 按 OR 分割
        or_parts = re.split(r"\s*OR\s*|；|;|\n", cleaned, flags=re.IGNORECASE)

        result = []
        for or_part in or_parts:
            or_part = or_part.strip()
            if not or_part:
                continue
            # 在每个 OR 组内按 AND / 中文逗号分割
            and_parts = re.split(r"\s*AND\s*|，|,", or_part, flags=re.IGNORECASE)
            and_parts = [p.strip() for p in and_parts if p.strip()]
            if and_parts:
                result.append(and_parts)

        if not result:
            return [[]]
        return result

    @classmethod
    def _parse_clause(cls, clause: str) -> Tuple[Optional[str], Optional[str]]:
        """
        解析单个 AND 子句，提取字段键和期望值。
        返回 (feature_key, expected_value)；若无法解析则返回 (None, None)
        """
        parts = re.split(r"(?:包含|含有|含|为|是|等于|=|:|：)", clause, maxsplit=1)
        if len(parts) < 2:
            return None, None

        left = parts[0].strip()
        right = parts[1].strip()
        if not left or not right:
            return None, None

        key = None
        for field, keywords in cls.FIELD_KEYWORDS.items():
            if any(kw in left for kw in keywords):
                key = field
                break

        return key, right

    def _condition_specificity(self, if_condition: str) -> int:
        """计算条件字符串中可解析字段-值对的总数量，作为特异性度量。"""
        if not if_condition:
            return 0
        count = 0
        for and_group in self._split_condition_clauses(if_condition):
            for clause in and_group:
                key, expected = self._parse_clause(clause)
                if key is not None and expected:
                    count += 1
        return count

    def _get_feature_value(self, features: Dict[str, Any], key: str) -> Optional[Any]:
        """按规则解析出的字段键从特征字典中取值。自动尝试别名查找。"""
        value = features.get(key)
        if value is not None and value != "":
            return value

        alias = self.RULE_FIELD_ALIASES.get(key)
        if alias:
            value = features.get(alias)
            if value is not None and value != "":
                return value

        return None

    def _match_freetext_clause(
        self,
        clause: str,
        features: Dict[str, Any],
        defect_type_val: str,
    ) -> Optional[str]:
        """
        自由文本子句匹配（v3.4 强化版）。返回匹配到的特征字段名，若不匹配返回 None。
        """
        # 第一阶段：检查缺陷类型匹配
        defect_match = (
            defect_type_val
            and self._has_common_substring(defect_type_val, clause)
            and not self.FREETEXT_BLOCKLIST_PATTERN.search(clause)
        )

        if defect_match:
            remaining = re.sub(
                re.escape(defect_type_val), ' ', clause, flags=re.IGNORECASE
            )
            tokens = re.split(r'[\s，。、；：（）\-\+]+', remaining)
            significant_tokens = [
                t.strip() for t in tokens
                if t.strip() and t not in self.FREETEXT_STOP_WORDS
            ]

            if not significant_tokens:
                return "defect_type"

            other_fields = {
                "media": str(features.get("media") or ""),
                "material": str(features.get("material") or ""),
                "device_type": str(features.get("device_type") or ""),
                "location": str(features.get("location") or ""),
                "component": str(features.get("component") or ""),
                "environment": str(features.get("environment") or ""),
            }

            for token in significant_tokens:
                for field, value in other_fields.items():
                    if value and self._has_common_substring(value, token):
                        return field
            return None

        # 第二阶段：其他字段匹配
        for field in ["media", "material", "device_type", "location", "component", "environment"]:
            value = features.get(field)
            if value is None or value == "":
                continue
            if self._has_common_substring(str(value), clause):
                return field

        return None

    def _match_single_condition(self, condition_str: str, features: Dict[str, Any]) -> bool:
        """匹配单个 AND 子句（v3.4 强化版）"""
        condition_str = condition_str.strip()
        if not condition_str:
            return True  # 空条件视为通过

        # 1. 尝试结构化解析
        key, expected = self._parse_clause(condition_str)
        if key is not None and expected:
            value = self._get_feature_value(features, key)
            if value is None or value == "":
                return False
            value_str = str(value)
            return self._has_common_substring(value_str, expected)

        # 2. 自由文本匹配（严格版）
        defect_type_val = str(features.get("defect_type") or features.get("type") or "")
        matched_field = self._match_freetext_clause(condition_str, features, defect_type_val)
        return matched_field is not None

    def _rule_matches_features(self, if_condition: str, features: Dict[str, Any]) -> bool:
        """
        判断规则条件是否与特征匹配（v3.4：支持 AND/OR 复合逻辑 + 严格自由文本）
        """
        if not if_condition or not if_condition.strip():
            return self.ALLOW_EMPTY_CONDITION_MATCH

        defect_type_val = str(features.get("defect_type") or features.get("type") or "")

        # 0. 否定前置检查
        if defect_type_val and self._condition_negates_defect_type(if_condition, defect_type_val):
            return False

        # 1. 缺陷类型强约束
        condition_mentions_defect_type = any(
            kw in if_condition for kw in self.DEFECT_TYPE_KEYWORDS
        )
        if condition_mentions_defect_type:
            if not defect_type_val:
                return False
            if not self._has_common_substring(defect_type_val, if_condition):
                return False

        # 2. 结构化条件匹配
        or_groups = self._split_condition_clauses(if_condition)
        if not or_groups:
            return False

        for and_group in or_groups:
            if not and_group:
                continue
            all_satisfied = True
            for clause in and_group:
                if not self._match_single_condition(clause, features):
                    all_satisfied = False
                    break
            if all_satisfied:
                return True

        return False

    # ---------- 诊断方法 ----------
    def diagnose_rule_match(self, rule_id: str, features: Dict[str, Any]) -> Dict[str, Any]:
        """打印单条规则与特征的匹配过程，返回诊断信息。"""
        rule = self.get_rule_by_id(rule_id)
        if rule is None:
            return {"rule_id": rule_id, "error": "规则不存在"}

        if_condition = rule.get("if_condition", "")
        defect_type_val = str(features.get("defect_type") or features.get("type") or "")

        or_groups = self._split_condition_clauses(if_condition)
        clause_groups = []
        for idx, and_group in enumerate(or_groups, 1):
            group_info = {"or_group_index": idx, "and_clauses": []}
            for clause in and_group:
                key, expected = self._parse_clause(clause)
                if key is not None and expected:
                    value = self._get_feature_value(features, key)
                    clause_info = {
                        "clause": clause,
                        "parsed_key": key,
                        "expected": expected,
                        "actual_value": value,
                        "satisfied": value is not None and self._has_common_substring(str(value), expected),
                    }
                else:
                    matched_field = self._match_freetext_clause(clause, features, defect_type_val)
                    clause_info = {
                        "clause": clause,
                        "parsed_key": None,
                        "expected": None,
                        "actual_value": None,
                        "satisfied": matched_field is not None,
                        "matched_field": matched_field,
                    }
                group_info["and_clauses"].append(clause_info)
            clause_groups.append(group_info)

        matched = self._rule_matches_features(if_condition, features)

        result = {
            "rule_id": rule_id,
            "rule_class": rule.get("rule_class", ""),
            "if_condition": if_condition,
            "features": features,
            "matched": matched,
            "defect_type_value": defect_type_val,
            "condition_mentions_defect_type": any(
                kw in if_condition for kw in self.DEFECT_TYPE_KEYWORDS
            ),
            "negates_defect_type": self._condition_negates_defect_type(if_condition, defect_type_val),
            "defect_type_has_common_substring": self._has_common_substring(defect_type_val, if_condition),
            "condition_structure": clause_groups,
        }

        import json as _json
        print(_json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return result

    # ---------- 规则调整应用 ----------

    def apply_rule_adjustments(
        self,
        base_s: int,
        base_o: int,
        base_d: int,
        rules: List[Dict[str, Any]],
        max_s_adj: Optional[int] = None,
        max_o_adj: Optional[int] = None,
        max_d_adj: Optional[int] = None,
    ) -> Tuple[int, int, int, List[Dict[str, Any]]]:
        """
        根据命中的规则列表，对 S/O/D 值进行调整。

        双重防御：
          1. 同一 rule_class 只应用第一条命中的规则（最具体规则）；
          2. S/O/D 总调整量分别封顶，默认上限 S=+5、O=+3、D=+2。
        """
        if max_s_adj is None:
            max_s_adj = self.DEFAULT_MAX_S_ADJ
        if max_o_adj is None:
            max_o_adj = self.DEFAULT_MAX_O_ADJ
        if max_d_adj is None:
            max_d_adj = self.DEFAULT_MAX_D_ADJ

        s_adj_total = 0
        o_adj_total = 0
        d_adj_total = 0

        applied_classes: set = set()
        applications: List[Dict[str, Any]] = []

        for rule in rules:
            rule_class = rule.get("rule_class", "")
            if rule_class and rule_class in applied_classes:
                logger.debug(
                    "规则 %s（%s）因同类规则已应用而跳过",
                    rule.get("rule_id"), rule_class,
                )
                continue

            action = rule.get("then_action", "")
            adjustments = self._parse_adjustment(action)

            s_delta = adjustments.get("S", 0)
            o_delta = adjustments.get("O", 0)
            d_delta = adjustments.get("D", 0)

            s_adj_total = max(-max_s_adj, min(max_s_adj, s_adj_total + s_delta))
            o_adj_total = max(-max_o_adj, min(max_o_adj, o_adj_total + o_delta))
            d_adj_total = max(-max_d_adj, min(max_d_adj, d_adj_total + d_delta))

            if rule_class:
                applied_classes.add(rule_class)

            applications.append({
                "rule_id": rule.get("rule_id"),
                "rule_class": rule_class,
                "adjustment_text": action,
                "applied_deltas": {"S": s_delta, "O": o_delta, "D": d_delta},
            })

        s = max(1, base_s + s_adj_total)
        o = max(1, base_o + o_adj_total)
        d = max(1, base_d + d_adj_total)

        logger.debug(
            "规则调整完成: %s → [%d/%d/%d]，实际应用 %d 条规则",
            f"{base_s}/{base_o}/{base_d}", s, o, d, len(applications),
        )

        return s, o, d, applications

    @staticmethod
    def _parse_adjustment(action_text: str) -> Dict[str, int]:
        """
        解析规则动作文本，提取 S/O/D 的调整量（v3.3 修复括号覆盖问题）。
        """
        adjustments = {"S": 0, "O": 0, "D": 0}
        if not action_text:
            return adjustments

        pattern_up = r"([SOD])上调(\d+)级"
        pattern_down = r"([SOD])下调(\d+)级"
        pattern_same = r"([SOD])不变"

        seen_factors = set()

        # 上调优先
        for match in re.finditer(pattern_up, action_text):
            factor, level = match.group(1), int(match.group(2))
            if factor not in seen_factors:
                adjustments[factor] = level
                seen_factors.add(factor)

        # 下调其次
        for match in re.finditer(pattern_down, action_text):
            factor, level = match.group(1), int(match.group(2))
            if factor not in seen_factors:
                adjustments[factor] = -level
                seen_factors.add(factor)

        # 不变最后
        for match in re.finditer(pattern_same, action_text):
            factor = match.group(1)
            if factor not in seen_factors:
                adjustments[factor] = 0
                seen_factors.add(factor)

        return adjustments

    # ---------- 相似案例检索 ----------

    def search_similar_cases(
        self,
        features: Dict[str, Any],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        根据特征信息检索相似失效案例。
        返回列表中的每个元素包含：
            case: 原始案例字典
            similarity_score: 相似度分数
            case_id: 案例ID
            measures: 规范化的措施列表（始终为 list）
        """
        similarity_scores = []
        for case in self.cases:
            score = self._calculate_similarity(case, features)
            if score > 0:
                similarity_scores.append((score, case))

        similarity_scores.sort(key=lambda x: x[0], reverse=True)

        top_cases = similarity_scores[:top_k]
        result = []
        for score, case in top_cases:
            measures = self._extract_measures(case)  # 统一提取措施
            result.append({
                "case": case,
                "similarity_score": score,
                "case_id": case.get("case_id", ""),
                "measures": measures,
            })
        return result

    def _extract_measures(self, case: Dict[str, Any]) -> List[str]:
        """
        从案例中提取整改措施，返回列表。
        优先使用 case["measures"]（若为列表且非空），
        否则从 case["corrective_measures"] 字符串分割。
        分割规则：仅按分号/换行，不按逗号。
        """
        # 检查是否有直接的 measures 字段且为列表
        measures = case.get("measures")
        if isinstance(measures, list) and measures:
            # 确保列表内元素都是字符串
            return [str(m).strip() for m in measures if str(m).strip()]

        # 如果 measures 是字符串（异常情况），也进行分割
        if isinstance(measures, str) and measures.strip():
            parts = re.split(r"[；;\n]", measures)
            return [p.strip() for p in parts if p.strip()]

        # 否则尝试从 corrective_measures 分割
        text = case.get("corrective_measures", "")
        if not text:
            return []
        parts = re.split(r"[；;\n]", str(text))
        return [p.strip() for p in parts if p.strip()]

    def _calculate_similarity(self, case: Dict[str, Any], features: Dict[str, Any]) -> float:
        """计算案例与特征的相似度分数（v3.5 增强字段映射）"""
        weights = {
            "device_type": 3,
            "defect_type": 3,
            "media": 2,
            "material": 1,
        }

        score = 0.0
        for feature_key, weight in weights.items():
            feature_value = features.get(feature_key)
            if not feature_value:
                continue

            # 根据 feature_key 获取案例中对应的字段
            case_field = self._map_feature_to_case_field(feature_key, case)
            if not case_field:
                continue

            case_value = case.get(case_field)
            if case_value and self._has_common_substring(str(feature_value), str(case_value)):
                score += weight

        # 额外相似度：环境、位置等
        extra_fields = ["environment", "location"]
        for field in extra_fields:
            feature_value = features.get(field)
            if feature_value:
                # 案例中可能没有完全同名字段，尝试常见字段
                for case_field in (field, "component", "equipment_subcategory", "equipment_category"):
                    case_val = case.get(case_field)
                    if case_val and self._has_common_substring(str(feature_value), str(case_val)):
                        score += 0.5
                        break

        return score

    @staticmethod
    def _map_feature_to_case_field(feature_key: str, case: Dict[str, Any] = None) -> Optional[str]:
        """
        将特征字段名映射到案例中的字段名。
        若提供了 case，可动态判断案例中存在的字段，提高匹配率。
        """
        mapping = {
            "device_type": "device_class",      # 向后兼容
            "defect_type": "failure_mode",
            "media": "media",
            "material": "material",
        }

        if feature_key == "device_type":
            # 优先使用设备分类相关字段
            if case is not None:
                for candidate in ("device_class", "equipment_category", "equipment_subcategory"):
                    if case.get(candidate):
                        return candidate
            return "device_class"  # 默认

        if feature_key == "defect_type":
            if case is not None:
                for candidate in ("failure_mode", "failure_phenomenon"):
                    if case.get(candidate):
                        return candidate
            return "failure_mode"  # 默认

        return mapping.get(feature_key)

    # ---------- 基线 S/O/D 提取 ----------

    def get_case_baseline_sod(
        self,
        device_class: Optional[str] = None,
        failure_mode: Optional[str] = None,
    ) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        """根据设备分类和失效模式提取案例的平均 S/O/D 值"""
        matching_cases = self.cases

        if device_class:
            matching_cases = [
                c for c in matching_cases
                if (
                    c.get("device_class") and self._has_common_substring(device_class, c["device_class"])
                ) or (
                    c.get("equipment_category") and self._has_common_substring(device_class, c["equipment_category"])
                ) or (
                    c.get("equipment_subcategory") and self._has_common_substring(device_class, c["equipment_subcategory"])
                )
            ]

        if failure_mode:
            matching_cases = [
                c for c in matching_cases
                if (
                    c.get("failure_mode") and self._has_common_substring(failure_mode, c["failure_mode"])
                ) or (
                    c.get("failure_phenomenon") and self._has_common_substring(failure_mode, c["failure_phenomenon"])
                )
            ]

        if not matching_cases:
            logger.warning("未找到匹配的案例用于提取基线 S/O/D")
            return None, None, None

        avg_s = round(sum(c.get("severity", 0) for c in matching_cases) / len(matching_cases))
        avg_o = round(sum(c.get("occurrence", 0) for c in matching_cases) / len(matching_cases))
        avg_d = round(sum(c.get("detection", 0) for c in matching_cases) / len(matching_cases))

        return avg_s, avg_o, avg_d

    # ---------- 统计与概览 ----------

    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_rules": len(self.rules),
            "total_cases": len(self.cases),
            "rule_classes": self.get_all_rule_classes(),
            "rule_class_counts": {
                rc: len(self.get_rules_by_class(rc)) for rc in self.get_all_rule_classes()
            },
        }

    def __repr__(self) -> str:
        return f"<KnowledgeBase rules={len(self.rules)} cases={len(self.cases)}>"


def get_knowledge_base() -> KnowledgeBase:
    """获取知识库单例实例"""
    return KnowledgeBase()


# ==================== 使用示例 ====================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    kb = KnowledgeBase()

    test_features = {
        "defect_type": "裂纹",
        "media": "液化石油气",
        "material": "12Cr1MoV",
        "device_type": "液化气储罐",
        "location": "筒体环焊缝",
    }
    matched = kb.match_expert_rules(test_features)
    print(f"匹配到 {len(matched)} 条规则：")
    for rule in matched[:5]:
        print(f"  - {rule['rule_id']} [{rule.get('rule_class')}]: {rule['then_action']}")

    if matched:
        adjusted = kb.apply_rule_adjustments(5, 3, 4, matched)
        print(f"调整后 S/O/D: {adjusted[0]}/{adjusted[1]}/{adjusted[2]}")
        print(f"实际应用规则数: {len(adjusted[3])}")

    similar = kb.search_similar_cases(test_features, top_k=3)
    print(f"找到 {len(similar)} 个相似案例")
    for item in similar:
        print(f"  - {item['case_id']} 相似度: {item['similarity_score']:.1f}")
        print(f"    整改措施: {item['measures']}")