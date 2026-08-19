"""
app/core/knowledge_base.py
知识库引擎模块

功能：
1. 加载并管理专家规则库和失效案例库；
2. 提供规则匹配、规则调整应用；
3. 提供相似案例检索和基线 S/O/D 提取；
4. 单例模式，全局唯一实例；
5. 支持路径配置和健壮的错误处理。
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------- 路径配置 ----------
# 优先从 app.core.config 导入 PROJECT_ROOT（若已定义）
try:
    from app.core.config import PROJECT_ROOT
except ImportError:
    # 后备方案：根据当前文件位置计算项目根目录
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 知识库文件默认路径
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
        """
        初始化知识库

        Args:
            rules_path: 专家规则 JSON 文件路径，默认使用 DEFAULT_RULES_PATH
            cases_path: 失效案例 JSON 文件路径，默认使用 DEFAULT_CASES_PATH
            auto_load: 是否立即加载数据，默认 True
        """
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
        """
        读取 JSON 文件，自动处理 UTF-8 编码（包括 BOM）

        Raises:
            FileNotFoundError: 文件不存在
            json.JSONDecodeError: JSON 格式错误
        """
        if not file_path.exists():
            raise FileNotFoundError(f"知识库文件不存在: {file_path}")

        with open(file_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)

    # ---------- 数据加载 ----------

    def load_rules(self) -> None:
        """加载专家规则库"""
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
        """加载失效案例库"""
        try:
            data = self._read_json(self.cases_path)
            if isinstance(data, dict):
                if "cases" in data and isinstance(data["cases"], list):
                    self.cases = data["cases"]
                else:
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
        """加载所有知识库（规则 + 案例）"""
        self.load_rules()
        self.load_cases()

    def reload(self) -> None:
        """重新加载所有知识库数据"""
        self.rules.clear()
        self.cases.clear()
        self.load_all()

    # ---------- 基础查询方法 ----------

    def get_rule_by_id(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """根据规则 ID 获取规则详情"""
        for rule in self.rules:
            if rule.get("rule_id") == rule_id:
                return rule
        return None

    def get_rules_by_class(self, rule_class: str) -> List[Dict[str, Any]]:
        """根据规则类别筛选规则"""
        return [rule for rule in self.rules if rule.get("rule_class") == rule_class]

    def get_all_rule_classes(self) -> List[str]:
        """获取所有规则类别（去重，保持顺序）"""
        seen = set()
        classes = []
        for rule in self.rules:
            rc = rule.get("rule_class")
            if rc and rc not in seen:
                seen.add(rc)
                classes.append(rc)
        return classes

    def get_case_by_id(self, case_id: str) -> Optional[Dict[str, Any]]:
        """根据案例 ID 获取案例详情"""
        for case in self.cases:
            if case.get("case_id") == case_id:
                return case
        return None

    # ---------- 字符串相似度辅助 ----------

    @staticmethod
    def _has_common_substring(s1: str, s2: str, min_len: int = 2) -> bool:
        """
        判断两个字符串是否包含长度至少为 min_len 的公共子串（忽略大小写）
        用于处理诸如“液氨”与“无水氨”等同义词匹配。
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

    # ---------- 专家规则匹配 ----------

    def match_expert_rules(self, features: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        根据特征信息匹配专家规则

        Args:
            features: 特征字典，可包含 media, material, device_type, environment,
                      operating_temperature, design_pressure 等键

        Returns:
            命中的规则列表（List[Dict]）
        """
        matched_rules = []
        for rule in self.rules:
            if_condition = rule.get("if_condition", "")
            if self._rule_matches_features(if_condition, features):
                matched_rules.append(rule)
        logger.debug(f"特征 {features} 匹配到 {len(matched_rules)} 条规则")
        return matched_rules

    def _rule_matches_features(self, if_condition: str, features: Dict[str, Any]) -> bool:
        """
        判断规则条件是否与特征匹配（基础版：所有非空特征值均能与条件产生公共子串）
        """
        # 仅检查 features 中非空的值
        for key, value in features.items():
            if value is None or value == "":
                continue
            # 将值转为字符串进行处理
            value_str = str(value)
            # 如果存在长度>=2的公共子串，则认为该特征满足
            if not self._has_common_substring(value_str, if_condition):
                return False
        return True

    # ---------- 规则调整应用 ----------

    def apply_rule_adjustments(
        self,
        base_s: int,
        base_o: int,
        base_d: int,
        rules: List[Dict[str, Any]],
    ) -> Tuple[int, int, int, List[Dict[str, Any]]]:
        """
        根据命中的规则列表，对 S/O/D 值进行调整

        Args:
            base_s, base_o, base_d: 基础 S/O/D 数值
            rules: 命中的规则列表

        Returns:
            (adjusted_s, adjusted_o, adjusted_d, rule_applications)
            rule_applications 为规则应用记录列表，每项包含 rule_id, rule_class, adjustment_text
        """
        s, o, d = base_s, base_o, base_d
        applications = []

        for rule in rules:
            action = rule.get("then_action", "")
            adjustments = self._parse_adjustment(action)

            # 应用调整
            s += adjustments.get("S", 0)
            o += adjustments.get("O", 0)
            d += adjustments.get("D", 0)

            applications.append({
                "rule_id": rule.get("rule_id"),
                "rule_class": rule.get("rule_class"),
                "adjustment_text": action,
            })

        # 确保评分不小于1（可根据实际评分范围调整）
        s = max(1, s)
        o = max(1, o)
        d = max(1, d)

        return s, o, d, applications

    @staticmethod
    def _parse_adjustment(action_text: str) -> Dict[str, int]:
        """
        解析规则动作文本，提取 S/O/D 的调整量

        示例文本: "S上调2级，O上调1级，D不变"
        返回: {"S": 2, "O": 1, "D": 0}
        """
        adjustments = {"S": 0, "O": 0, "D": 0}
        if not action_text:
            return adjustments

        # 匹配 "S上调2级" / "O下调1级" / "D不变"
        pattern_up = r"([SOD])上调(\d+)级"
        pattern_down = r"([SOD])下调(\d+)级"
        pattern_same = r"([SOD])不变"

        for match in re.finditer(pattern_up, action_text):
            factor, level = match.group(1), int(match.group(2))
            adjustments[factor] = level
        for match in re.finditer(pattern_down, action_text):
            factor, level = match.group(1), int(match.group(2))
            adjustments[factor] = -level
        for match in re.finditer(pattern_same, action_text):
            factor = match.group(1)
            adjustments[factor] = 0  # 不变即覆盖之前可能的值，保持为0

        return adjustments

    # ---------- 相似案例检索 ----------

    def search_similar_cases(
        self,
        features: Dict[str, Any],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        根据特征信息检索相似失效案例

        Args:
            features: 特征字典，可包含 defect_type, location, media, material,
                      device_type, wall_thickness, depth 等键
            top_k: 返回前 k 个相似案例

        Returns:
            相似案例列表，每个元素包含 case 数据、相似度分数、case_id 和措施
            [{"case": {...}, "similarity_score": float, "case_id": str, "measures": [...]}, ...]
        """
        similarity_scores = []
        for case in self.cases:
            score = self._calculate_similarity(case, features)
            if score > 0:
                similarity_scores.append((score, case))

        # 按分数降序排序
        similarity_scores.sort(key=lambda x: x[0], reverse=True)

        top_cases = similarity_scores[:top_k]
        result = []
        for score, case in top_cases:
            result.append({
                "case": case,
                "similarity_score": score,
                "case_id": case.get("case_id", ""),
                "measures": case.get("measures", []),
            })
        return result

    def _calculate_similarity(self, case: Dict[str, Any], features: Dict[str, Any]) -> float:
        """
        计算案例与特征的相似度得分（基于字段匹配加权）
        """
        # 权重定义：不同字段对相似度的贡献不同
        weights = {
            "device_type": 3,   # features 中的设备类型 vs case 的 device_class
            "defect_type": 3,   # features 中的缺陷类型 vs case 的 failure_mode
            "media": 2,         # features 中的介质 vs case 的 media 字段
            "material": 1,      # features 中的材质 vs case 的 material 字段
        }

        score = 0.0
        for feature_key, weight in weights.items():
            feature_value = features.get(feature_key)
            if not feature_value:
                continue

            # 根据 feature_key 映射到案例中的字段名
            case_field = self._map_feature_to_case_field(feature_key)
            if not case_field:
                continue

            case_value = case.get(case_field)
            if case_value and self._has_common_substring(str(feature_value), str(case_value)):
                score += weight

        # 额外处理一些通用字段（如环境、温度等），权重较低
        extra_fields = ["environment", "location"]
        for field in extra_fields:
            feature_value = features.get(field)
            if feature_value:
                if case.get(field) and self._has_common_substring(str(feature_value), str(case.get(field))):
                    score += 0.5

        return score

    @staticmethod
    def _map_feature_to_case_field(feature_key: str) -> Optional[str]:
        """将特征键映射到案例字段名"""
        mapping = {
            "device_type": "device_class",
            "defect_type": "failure_mode",
            "media": "media",
            "material": "material",
        }
        return mapping.get(feature_key)

    # ---------- 基线 S/O/D 提取 ----------

    def get_case_baseline_sod(
        self,
        device_class: Optional[str] = None,
        failure_mode: Optional[str] = None,
    ) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        """
        从案例库中提取匹配案例的基准 S/O/D 值（平均值取整）

        Args:
            device_class: 设备大类（如 "移动式压力容器"），可选
            failure_mode: 失效模式（如 "点蚀"），可选

        Returns:
            (avg_s, avg_o, avg_d) 三元组；若无匹配案例则返回 (None, None, None)
        """
        matching_cases = self.cases

        if device_class:
            matching_cases = [
                c for c in matching_cases
                if c.get("device_class") and self._has_common_substring(device_class, c["device_class"])
            ]

        if failure_mode:
            matching_cases = [
                c for c in matching_cases
                if c.get("failure_mode") and self._has_common_substring(failure_mode, c["failure_mode"])
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
        """返回知识库统计信息"""
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


# 便捷的单例获取函数（可选）
def get_knowledge_base() -> KnowledgeBase:
    """获取知识库单例实例"""
    return KnowledgeBase()


# ==================== 使用示例 ====================
if __name__ == "__main__":
    # 配置日志输出
    logging.basicConfig(level=logging.INFO)

    # 获取知识库实例（自动加载）
    kb = KnowledgeBase()

    # 测试规则匹配
    test_features = {
        "media": "液氨",
        "material": "Q345R",
        "device_type": "移动式压力容器",
    }
    matched = kb.match_expert_rules(test_features)
    print(f"匹配到 {len(matched)} 条规则：")
    for rule in matched[:3]:
        print(f"  - {rule['rule_id']} {rule['rule_class']}: {rule['then_action']}")

    # 测试调整应用
    if matched:
        adjusted = kb.apply_rule_adjustments(3, 4, 2, matched[:2])
        print(f"调整后 S/O/D: {adjusted[0]}/{adjusted[1]}/{adjusted[2]}")

    # 测试相似案例检索
    similar = kb.search_similar_cases(test_features, top_k=3)
    print(f"找到 {len(similar)} 个相似案例")
    for item in similar:
        print(f"  - {item['case_id']} 相似度: {item['similarity_score']:.1f}")