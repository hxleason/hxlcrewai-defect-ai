# defect_grader.py
"""
压力容器缺陷安全等级计算器 & FMEA 确定性格算盘
- 保留原有的 grade_defect 函数（输出 1~5 级）
- 新增 calc_fmea 函数（输出 S/O/D/RPN 等完整字段）
- 新增 diagnose_causes 函数（输出非 LLM 的可能原因）
"""

def grade_crack(depth_mm: float, wall_thickness_mm: float, length_mm: float = 0) -> int:
    """
    裂纹类缺陷安全等级（1~5级，5级最高）
    规则：深度与壁厚的比值决定基础等级，长度修正。
    """
    ratio = depth_mm / wall_thickness_mm

    # 基础等级
    if ratio >= 0.5:
        base = 5
    elif ratio >= 0.3:
        base = 4
    elif ratio >= 0.15:
        base = 3
    elif ratio >= 0.05:
        base = 2
    else:
        base = 1

    # 长度修正：长裂纹提级（长度大于50mm时等级+1，但不超过5级）
    if length_mm > 50 and base < 5:
        base += 1
    return min(base, 5)


def grade_porosity(diameter_mm: float, density: str) -> int:
    """
    气孔类缺陷安全等级
    density: '密集' / '分散'
    """
    if density == '密集':
        if diameter_mm > 3:
            return 4
        elif diameter_mm > 1.5:
            return 3
        else:
            return 2
    else:  # 分散
        if diameter_mm > 5:
            return 3
        elif diameter_mm >= 2:   # >=2 时返回2，否则返回1
            return 2
        else:
            return 1


def grade_defect(defect_type: str, dimensions: dict,
                 wall_thickness_mm: float) -> int:
    """
    根据缺陷类型和尺寸计算安全等级（1~5级）。

    参数:
        defect_type: 缺陷类型字符串，例如 '裂纹'、'气孔'、'针孔' 等
        dimensions: 字典，包含必要的尺寸参数，例如：
            - 裂纹: {'length': 15, 'depth': 2}
            - 气孔: {'diameter': 4, 'density': '密集'}  或 {'length': 4, 'density': '密集'}（兼容长度字段）
        wall_thickness_mm: 设备壁厚（毫米）

    返回: 安全等级（1~5），未知类型默认返回3（需人工复核）
    """
    t = defect_type.strip()
    if '裂纹' in t:
        depth = dimensions.get('depth', 0)
        length = dimensions.get('length', 0)
        return grade_crack(depth, wall_thickness_mm, length)
    elif '气孔' in t or '针孔' in t:
        # 兼容 diameter 或 length 作为尺寸字段
        diam = dimensions.get('diameter', dimensions.get('length', 0))
        density = dimensions.get('density', '分散')
        return grade_porosity(diam, density)
    else:
        # 未知类型默认返回3（需人工复核）
        return 3


# ========================= 新增 FMEA 计算器 =========================

def calc_fmea(defect_type: str, dimensions: dict,
              wall_thickness_mm: float) -> dict:
    """
    基于 grade_defect 的安全等级 + 缺陷特性，生成 FMEA 核心参数，无任何 LLM 参与。

    返回字典包含:
        - S (严重度, 1-10)
        - O (发生度, 1-10)
        - D (探测度, 1-10)
        - RPN (风险优先数 = S*O*D)
        - risk_level (风险等级字符串: 极高/高/中/低)
        - level (原始安全等级, 1-5)
        - explanation (计算说明)
    """
    level = grade_defect(defect_type, dimensions, wall_thickness_mm)

    # 严重度 S 映射（等级越高，S 越大）
    if level >= 5:
        S = 9
    elif level == 4:
        S = 7
    elif level == 3:
        S = 5
    elif level == 2:
        S = 3
    else:
        S = 2   # 等级1，极轻微，S=2（非零）

    # 发生度 O：基于缺陷类型的固有发生频率（经验值）
    t = defect_type.strip()
    if '裂纹' in t:
        O = 4
    elif '气孔' in t or '针孔' in t:
        O = 6
    else:
        O = 5

    # 探测度 D：基于常规检测手段的检出难度
    if '裂纹' in t:
        D = 5          # 表面裂纹较易检出，但内部裂纹稍难，综合取5
    elif '气孔' in t or '针孔' in t:
        D = 4          # 气孔一般较明显
    else:
        D = 5

    rpn = S * O * D

    if rpn >= 200:
        risk_level = "极高风险"
    elif rpn >= 100:
        risk_level = "高风险"
    elif rpn >= 50:
        risk_level = "中风险"
    else:
        risk_level = "低风险"

    return {
        "S": S,
        "O": O,
        "D": D,
        "RPN": rpn,
        "risk_level": risk_level,
        "level": level,
        "explanation": (
            f"安全等级{level} → 严重度S={S}, 发生度O={O}, 探测度D={D}, RPN={rpn}"
        )
    }


def diagnose_causes(defect_type: str) -> list:
    """
    非 LLM 的缺陷原因库，根据缺陷类型返回可能原因列表。
    后续可扩展为链接标准条款、维修建议等。
    """
    t = defect_type.strip()
    if '裂纹' in t:
        return [
            "焊接残余应力未消除",
            "材料局部淬硬倾向",
            "疲劳载荷",
            "氢致裂纹"
        ]
    elif '气孔' in t or '针孔' in t:
        return [
            "焊接保护气体不足",
            "焊材/母材表面潮湿",
            "坡口清理不彻底",
            "电弧电压不稳定"
        ]
    elif '腐蚀' in t:
        return [
            "介质中氯离子浓度过高",
            "涂层/衬里破损",
            "长期潮湿环境",
            "电化学腐蚀"
        ]
    else:
        return [
            "材料老化",
            "操作维护不当",
            "设计裕度不足",
            "突发过载"
        ]