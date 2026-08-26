"""
生成 PWHT 知识库 JSON（v2 · 与 pwht_knowledge_base.py 完全对齐）
用法：python scripts/generate_pwht_kb_v2.py
输出目录：data/pwht_kb/
"""
import json
import os
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "pwht_kb"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(filename: str, data: dict) -> None:
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 已写入：{filepath}")


# =====================================================
# 1. material_groups.json
#    → 对应 get_material_group_info()
# =====================================================
material_groups = {
    "groups": [
        {
            "group_id": "Fe-1",
            "group_name": "非合金钢",
            "subgroups": [
                {"subgroup_id": "Fe-1-1", "subgroup_name": "碳素钢"},
                {"subgroup_id": "Fe-1-2", "subgroup_name": "低温用钢"},
                {"subgroup_id": "Fe-1-3", "subgroup_name": "其他非合金钢"},
            ],
        },
        {
            "group_id": "Fe-3",
            "group_name": "低合金钢",
            "subgroups": [
                {"subgroup_id": "Fe-3-1", "subgroup_name": "Mn钢"},
                {"subgroup_id": "Fe-3-2", "subgroup_name": "Mn-V钢"},
                {"subgroup_id": "Fe-3-3", "subgroup_name": "Cr-Mo低合金钢"},
            ],
        },
        {
            "group_id": "Fe-4",
            "group_name": "Cr-Mo耐热钢",
            "subgroups": [
                {"subgroup_id": "Fe-4-1", "subgroup_name": "Cr-Mo(<1.5Cr)"},
                {"subgroup_id": "Fe-4-2", "subgroup_name": "Cr-Mo(1.5Cr~2.5Cr)"},
            ],
        },
        {
            "group_id": "Fe-5A",
            "group_name": "高Cr-Mo钢",
            "subgroups": [
                {"subgroup_id": "Fe-5A", "subgroup_name": "高Cr-Mo(2.5Cr~4Cr)"},
            ],
        },
        {
            "group_id": "Fe-5B",
            "group_name": "Cr-Mo-V钢",
            "subgroups": [
                {"subgroup_id": "Fe-5B-1", "subgroup_name": "中铬(<4Cr)"},
                {"subgroup_id": "Fe-5B-2", "subgroup_name": "9Cr马氏体"},
            ],
        },
        {
            "group_id": "Fe-6",
            "group_name": "铁素体不锈钢",
            "subgroups": [{"subgroup_id": "Fe-6", "subgroup_name": "铁素体不锈钢"}],
        },
        {
            "group_id": "Fe-7",
            "group_name": "奥氏体不锈钢(304系列)",
            "subgroups": [{"subgroup_id": "Fe-7", "subgroup_name": "奥氏体不锈钢(304系列)"}],
        },
        {
            "group_id": "Fe-8",
            "group_name": "奥氏体不锈钢(316系列等)",
            "subgroups": [{"subgroup_id": "Fe-8", "subgroup_name": "奥氏体不锈钢(316系列等)"}],
        },
        {
            "group_id": "Fe-9B",
            "group_name": "双相不锈钢",
            "subgroups": [{"subgroup_id": "Fe-9B", "subgroup_name": "双相不锈钢"}],
        },
        {
            "group_id": "Fe-10I",
            "group_name": "超级铁素体不锈钢",
            "subgroups": [{"subgroup_id": "Fe-10I", "subgroup_name": "超级铁素体不锈钢"}],
        },
        {
            "group_id": "Fe-11A",
            "group_name": "镍基合金",
            "subgroups": [{"subgroup_id": "Fe-11A", "subgroup_name": "镍基合金"}],
        },
    ]
}

# =====================================================
# 辅助函数：构建单个材料的 params 条目
# =====================================================
def make_param(
    material_group: str,
    min_temp: int,
    max_temp: int,
    hold_25mm: str,
    hold_50mm: str,
    hold_125mm: str,
    min_hours: float,
) -> dict:
    """生成 get_recommended_params 期望的 param 结构"""
    return {
        "material_group": material_group,
        "min_holding_temp_c": min_temp,
        "max_holding_temp_c": max_temp,
        "holding_time_rules": {
            "le_25mm": {
                "formula": hold_25mm,
                "min_hours": min_hours,
            },
            "gt_25_le_50mm": {
                "formula": hold_50mm,
                "min_hours": min_hours,
            },
            "gt_50_le_125mm": {
                "formula": hold_125mm,
                "min_hours": min_hours,
            },
            "gt_125mm": {
                "formula": hold_125mm,
                "min_hours": min_hours,
            },
        },
    }


# =====================================================
# 2. boiler_params.json  → get_recommended_params(equipment_type="boiler")
# =====================================================
boiler_params = {
    "params": [
        make_param("Fe-1-1", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-1-2", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-1-3", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-3-1", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-3-2", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-3-3", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-4-1", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-4-2", 650, 700, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-5A", 650, 700, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-5B-1", 650, 700, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        # Fe-5B-2 特殊：≤50mm 至少保温 0.5h
        make_param("Fe-5B-2", 650, 760, "max(δ/25, 0.5)", "2+(δ-50)/100", "2+(δ-50)/100", 0.5),
        make_param("Fe-6", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-7", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-8", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-9B", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-10I", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-11A", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
    ]
}

# =====================================================
# 3. vessel_params.json   → get_recommended_params(equipment_type="vessel")
# 与 boiler 基本一致，压力容器在 25-50mm 段略有差异，但统一按锅炉三段式
# =====================================================
vessel_params = {
    "params": [
        make_param("Fe-1-1", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-1-2", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-1-3", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-3-1", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-3-2", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-3-3", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-4-1", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-4-2", 650, 700, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-5A", 650, 700, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-5B-1", 650, 700, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-5B-2", 650, 760, "max(δ/25, 0.5)", "2+(δ-50)/100", "2+(δ-50)/100", 0.5),
        make_param("Fe-6", 620, 680, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-7", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-8", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-9B", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-10I", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
        make_param("Fe-11A", 600, 650, "max(δ/25, 0.25)", "2+(δ-50)/100", "2+(δ-50)/100", 0.25),
    ]
}

# =====================================================
# 4. temp_reduction_compensation.json
#    → 目前无直接调用，但保留占位结构
# =====================================================
temp_reduction_compensation = {
    "rules": [
        {"under_min_temp_by_c": 10, "compensation_min": 10},
        {"under_min_temp_by_c": 20, "compensation_min": 20},
        {"under_min_temp_by_c": 30, "compensation_min": 30},
        {"under_min_temp_by_c": 50, "compensation_min": 50},
        {"under_min_temp_by_c": 100, "compensation_min": 100},
    ]
}

# =====================================================
# 5. dissimilar_steel_temps.json
# =====================================================
dissimilar_steel_temps = {
    "pairs": [
        {"pair": "Fe-1-1+Fe-3-1", "min_temp": 620, "max_temp": 680},
        {"pair": "Fe-1-1+Fe-4-1", "min_temp": 620, "max_temp": 680},
        {"pair": "Fe-1-1+Fe-4-2", "min_temp": 650, "max_temp": 700},
        {"pair": "Fe-3-1+Fe-4-1", "min_temp": 650, "max_temp": 700},
        {"pair": "Fe-3-1+Fe-4-2", "min_temp": 650, "max_temp": 700},
        {"pair": "Fe-1-1+Fe-7",   "min_temp": 600, "max_temp": 650},
        {"pair": "Fe-3-1+Fe-7",   "min_temp": 600, "max_temp": 650},
        {"pair": "Fe-1-1+Fe-11A", "min_temp": 600, "max_temp": 650},
    ]
}

# =====================================================
# 6. heating_cooling_limits.json
#    → 对应 get_heating_cooling_limits()
# =====================================================
heating_cooling_limits = {
    "furnace_pwht": {
        "entry_temp_max_c": 400,
        "heating_rate": {
            "above_400c": {
                "formula": "6250/δ",
                "max_rate": 220,
            }
        },
        "cooling_rate": {
            "above_400c": {
                "formula": "8000/δ",
                "max_rate": 280,
            }
        },
        "temperature_uniformity": {
            "soaking_period_max_diff_c": 80,
        },
    }
}

# =====================================================
# 7. soak_band_rules.json
#    → 对应 get_band_widths()
# =====================================================
soak_band_rules = {
    "single_heating_local_pwht": {
        "heating_band_width": {
            "double_side_insulation": {
                "formula": "W_HB ≥ 3√(Rδ)",
                "factor": 3.0,
            },
            "single_side_insulation": {
                "formula": "W_HB ≥ 5√(Rδ)",
                "factor": 5.0,
            },
        },
        "gradient_control_band_width": {
            "description": "梯度控制带宽度取加热带宽度的2~3倍",
            "factor": 2.5,
        },
    }
}

# =====================================================
# 8. delta_pwht_rules.json
#    → 对应 get_delta_pwht()
# =====================================================
delta_pwht_rules = {
    "rules": [
        {
            "condition": "对接接头",
            "note": "等厚对接时δPWHT取较薄件厚度",
        },
        {
            "condition": "角接接头",
            "note": "角接接头δPWHT取焊缝计算厚度",
        },
        {
            "condition": "非承压件与承压件连接",
            "note": "δPWHT取承压件厚度",
        },
        {
            "condition": "管座或接管",
            "note": "δPWHT取管座或接管厚度",
        },
    ]
}

# =====================================================
# 9. special_material_rules.json
#    → 对应 check_special_materials()
# 注意: 每个 rule 必须有 material_group, title, section, requirements
# =====================================================
special_material_rules = {
    "rules": [
        {
            "material_group": "Fe-5B-2",
            "title": "9Cr马氏体耐热钢附加要求",
            "section": "GB/T 30583-2026 第4.4.15 e)条",
            "requirements": "PWHT 后应立即进行冷弯检验；焊缝金属 Nb+V 含量应满足规定；建议最高保温温度 760℃",
        },
        {
            "material_group": "Fe-9B",
            "title": "双相不锈钢附加要求",
            "section": "GB/T 30583-2026 第4.4.15 f)条",
            "requirements": "PWHT 应避免在 450-950℃ 长时间停留；升降温速率应严格控制；PWHT 后应进行铁素体含量测量",
        },
        {
            "material_group": "Fe-10I",
            "title": "超级铁素体不锈钢附加要求",
            "section": "GB/T 30583-2026 第4.4.15 f)条",
            "requirements": "PWHT 温度下限应≥650℃；冷却速率应缓慢进行",
        },
        {
            "material_group": "Fe-11A",
            "title": "镍基合金附加要求",
            "section": "GB/T 30583-2026 第4.4.15 g)条",
            "requirements": "PWHT 温度范围应根据具体合金确定；应避免在敏感温度区间长期停留",
        },
        {
            "material_group": "Fe-6",
            "title": "铁素体不锈钢附加要求",
            "section": "GB/T 30583-2026 第4.4.15 f)条",
            "requirements": "关注冷却速率和防脆化要求",
        },
        {
            "material_group": "Fe-7",
            "title": "奥氏体不锈钢附加要求",
            "section": "GB/T 30583-2026 第4.4.15 f)条",
            "requirements": "通常不要求PWHT；必要时进行固溶处理",
        },
        {
            "material_group": "Fe-8",
            "title": "奥氏体不锈钢(316系列)附加要求",
            "section": "GB/T 30583-2026 第4.4.15 f)条",
            "requirements": "通常不要求PWHT；必要时进行固溶处理",
        },
    ]
}

# =====================================================
# 10. exemption_rules.json
#    → 对应 get_exemption_rules() / check_exemption()
# =====================================================
exemption_rules = {
    "rules": [
        {
            "condition": "壁厚≤20mm",
            "material_group": "Fe-1-1",
            "equipment": "压力容器",
            "exempt": True,
            "note": "满足强度条件下的薄壁碳钢可免做PWHT",
        },
        {
            "condition": "壁厚≤30mm",
            "material_group": "Fe-1-1",
            "equipment": "非承压构件",
            "exempt": True,
            "note": "非承压构件可放宽至30mm",
        },
        {
            "condition": "补焊深度≤10mm",
            "material_group": "任意",
            "equipment": "各类承压设备",
            "exempt": True,
            "note": "浅层补焊通常无需PWHT，但碳当量CE≥0.45%时应做",
        },
        {
            "condition": "奥氏体不锈钢常规模焊",
            "material_group": "Fe-7",
            "equipment": "压力容器",
            "exempt": True,
            "note": "奥氏体不锈钢通常不要求PWHT，必要时固溶处理",
        },
    ]
}

# =====================================================
# 写入全部 10 个文件
# =====================================================
files = {
    "material_groups.json": material_groups,
    "boiler_params.json": boiler_params,
    "vessel_params.json": vessel_params,
    "temp_reduction_compensation.json": temp_reduction_compensation,
    "dissimilar_steel_temps.json": dissimilar_steel_temps,
    "heating_cooling_limits.json": heating_cooling_limits,
    "soak_band_rules.json": soak_band_rules,
    "delta_pwht_rules.json": delta_pwht_rules,
    "special_material_rules.json": special_material_rules,
    "exemption_rules.json": exemption_rules,
}

for filename, data in files.items():
    write_json(filename, data)

print(f"\n✅ 全部 {len(files)} 个文件已生成到 {OUTPUT_DIR}")