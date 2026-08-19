"""
app/core/pwht_knowledge_base.py
GB/T 30583-2026 焊后热处理标准知识库引擎

提供：
  - get_recommended_params()   : 根据材料组别+设备类型推荐保温温度/时间
  - get_heating_cooling_limits(): 升降温速率限值查询
  - get_band_widths()          : 均温带/加热带/梯度控制带宽度计算
  - get_delta_pwht()           : δPWHT 取值
  - check_special_materials()  : 特殊材料附加要求检查
  - check_exemption()          : 免除热处理判断
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import PWHT_KB_FOLDER

logger = logging.getLogger(__name__)

# 需要加载的 JSON 文件名
KB_FILES = {
    "material_groups": "material_groups.json",
    "boiler_params": "boiler_params.json",
    "vessel_params": "vessel_params.json",
    "temp_compensation": "temp_reduction_compensation.json",
    "dissimilar_temps": "dissimilar_steel_temps.json",
    "heating_cooling": "heating_cooling_limits.json",
    "band_widths": "soak_band_rules.json",
    "delta_pwht_rules": "delta_pwht_rules.json",
    "special_materials": "special_material_rules.json",
    "exemption_rules": "exemption_rules.json",
}


class PWHTKnowledgeBase:
    """PWHT 标准知识库（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._data: Dict[str, Any] = {}
        self._load_all()
        self._initialized = True

    # ---------- 数据加载 ----------

    def _load_json(self, filename: str) -> Any:
        filepath = Path(PWHT_KB_FOLDER) / filename
        if not filepath.exists():
            logger.warning(f"PWHT 知识库文件不存在: {filepath}")
            return None
        with open(filepath, "r", encoding="utf-8-sig") as f:
            return json.load(f)

    def _load_all(self):
        for key, filename in KB_FILES.items():
            data = self._load_json(filename)
            if data is not None:
                self._data[key] = data
                logger.debug(f"已加载 PWHT 知识库: {filename}")
        logger.info(f"PWHT 知识库加载完成，共 {len(self._data)} 个数据模块")

    def reload(self):
        self._data.clear()
        self._load_all()

    # ---------- 核心查询接口 ----------

    def get_material_group_info(self, group_id: str) -> Optional[Dict]:
        """根据材料组别 ID（如'Fe-5B-2'）返回材料信息"""
        for group in self._data.get("material_groups", {}).get("groups", []):
            if group["group_id"] == group_id:
                return group
            for subgroup in group.get("subgroups", []):
                if subgroup.get("subgroup_id") == group_id:
                    return subgroup
        return None

    def get_recommended_params(
        self,
        material_group: str,
        equipment_type: str = "vessel",  # "boiler" 或 "vessel"
        delta_pwht: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        获取推荐 PWHT 参数。
        - material_group: 材料组别（如 "Fe-4-2"、"Fe-5B-2"）
        - equipment_type: "boiler"（锅炉）或 "vessel"（压力容器）
        - delta_pwht: 焊后热处理厚度（mm），用于计算最短保温时间
        """
        params_table = self._data.get(
            "boiler_params" if equipment_type == "boiler" else "vessel_params", {}
        )
        params_list = params_table.get("params", [])

        for param in params_list:
            if param["material_group"] == material_group:
                result = {
                    "material_group": material_group,
                    "min_holding_temp_c": param.get("min_holding_temp_c"),
                    "max_holding_temp_c": param.get("max_holding_temp_c"),
                    "equipment_type": equipment_type,
                }
                # 计算保温时间
                if delta_pwht is not None:
                    holding_time = self._calc_holding_time(param, delta_pwht)
                    result["min_holding_time_h"] = holding_time
                return result

        return {
            "material_group": material_group,
            "error": f"未找到材料组别 {material_group} 在 {equipment_type} 参数表中的记录",
        }

    def _calc_holding_time(self, param: Dict, delta_pwht: float) -> float:
        """根据 δPWHT 计算最短保温时间"""
        rules = param.get("holding_time_rules", {})
        min_hours = None

        if delta_pwht <= 25:
            rule = rules.get("le_25mm", {})
            formula = rule.get("formula", "")
        elif delta_pwht <= 50:
            rule = rules.get("gt_25_le_50mm", {})
            formula = rule.get("formula", "")
        elif delta_pwht <= 125:
            rule = rules.get("gt_50_le_125mm", {})
            formula = rule.get("formula", "")
        else:
            rule = rules.get("gt_125mm", {})
            formula = rule.get("formula", "")

        try:
            # 简单公式计算（δPWHT/25、2+(δPWHT-50)/100 等）
            t = delta_pwht
            calculated = eval(formula, {"δPWHT": t, "δ": t})
            min_hours = rule.get("min_hours")
            if min_hours is not None:
                return max(calculated, min_hours)
            return calculated
        except Exception:
            return rule.get("min_hours", 0)

    def get_heating_cooling_limits(
        self,
        delta_pwht: float,
        is_boiler: bool = False,
    ) -> Dict[str, Any]:
        """获取升降温速率限值"""
        hc = self._data.get("heating_cooling", {})
        furnace = hc.get("furnace_pwht", {})

        def calc_formula(formula: str, delta: float) -> float:
            try:
                return eval(formula, {"δ": delta})
            except Exception:
                return 0

        heating = furnace.get("heating_rate", {}).get("above_400c", {})
        cooling = furnace.get("cooling_rate", {}).get("above_400c", {})

        return {
            "heating_rate_max_c_per_h": min(calc_formula(heating.get("formula", ""), delta_pwht), heating.get("max_rate", 220)),
            "cooling_rate_max_c_per_h": min(calc_formula(cooling.get("formula", ""), delta_pwht), cooling.get("max_rate", 280)),
            "entry_temp_max_c": furnace.get("entry_temp_max_c", 400),
            "soaking_max_temp_diff_c": furnace.get("temperature_uniformity", {}).get("soaking_period_max_diff_c", 80),
        }

    def get_band_widths(
        self,
        shell_radius: float,
        shell_thickness: float,
        insulation: str = "double",  # "double" 或 "single"
    ) -> Dict[str, Any]:
        """获取局部热处理带宽推荐值（附录B/C）"""
        import math
        bw = self._data.get("band_widths", {})
        single = bw.get("single_heating_local_pwht", {})

        result = {}
        if insulation == "double":
            hb_rule = single.get("heating_band_width", {}).get("double_side_insulation", {})
        else:
            hb_rule = single.get("heating_band_width", {}).get("single_side_insulation", {})

        formula = hb_rule.get("formula", "W_HB ≥ 3√(Rδ)")
        # 解析公式：提取倍数
        import re
        match = re.search(r"(\d+)\s*√\(Rδ\)", formula)
        if match:
            factor = float(match.group(1))
            w_hb = factor * math.sqrt(shell_radius * shell_thickness)
            w_gcb = 2.5 * w_hb  # (2~3) 取中值
            result["heating_band_width_mm"] = round(w_hb, 1)
            result["gradient_control_band_width_mm"] = round(w_gcb, 1)

        return result

    def get_delta_pwht(self, joint_type: str, **kwargs) -> Optional[Dict]:
        """获取 δPWHT 取值规则"""
        rules = self._data.get("delta_pwht_rules", {})
        for rule in rules.get("rules", []):
            if joint_type in rule.get("condition", ""):
                return rule
        return None

    def check_special_materials(self, material_group: str) -> List[Dict]:
        """检查特殊材料附加要求"""
        specials = self._data.get("special_materials", {}).get("rules", [])
        matched = []
        for rule in specials:
            mg = rule.get("material_group", "")
            if material_group in mg or mg in material_group:
                matched.append(rule)
        return matched

    def get_exemption_rules(self) -> List[Dict]:
        """获取免除热处理相关规则"""
        return self._data.get("exemption_rules", {}).get("rules", [])

    def check_exemption(self, material_group: str, temp_c: float) -> bool:
        """
        判断某热过程是否可视为 PWHT（4.1.4条）
        - 非合金钢/低合金钢: <490℃ 不作为 PWHT
        - 高合金钢: <315℃ 不作为 PWHT
        """
        high_alloy_groups = ["Fe-6", "Fe-7", "Fe-8", "Fe-10I", "Fe-10H", "Fe-11A"]
        is_high_alloy = any(g in material_group for g in high_alloy_groups)
        threshold = 315 if is_high_alloy else 490
        return temp_c < threshold


def get_pwht_kb() -> PWHTKnowledgeBase:
    """获取 PWHT 知识库单例"""
    return PWHTKnowledgeBase()


# 兼容旧式获取方式
def get_knowledge_base() -> "PWHTKnowledgeBase":
    """别名，与 calling convention 兼容"""
    return get_pwht_kb()