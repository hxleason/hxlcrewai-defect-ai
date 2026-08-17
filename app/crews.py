"""
app/crews.py – 缺陷提取与 FMEA 分析双智能体（终极版 v7.3）

核心功能：
1. 缺陷提取（稳健 JSON 解析，兼容不支持 response_format 的 LLM）
2. 物理属性预评分（壁厚/深度/数量 + 服役年限/检测方法）
3. FMEA 分析（LLM 精修预评分，输出 RPN / 风险等级 / 建议）
4. AP 行动优先级自动计算（M‑4）
5. 高风险闸门（M‑3）：自动标记需审核的缺陷
6. 端到端流水线（run_full_fmea）
7. 向后兼容旧版 API（run_fmea_evaluation / run_full_fmea_evaluation）

更新日志 v7.3：
- 强化 extract_defects 的 Prompt，增加 JSON Schema 示例，强制要求 defect_type 和 original_text
- 配合 schemas.py v2.1 的 model_validator，实现 type→defect_type 自动映射和缺失字段兜底
- 彻底消除因字段名不一致导致的 Pydantic 验证错误

⚠️ 数据流兼容说明：
  本模块输出的 DefectBase 模型包含字段 `defect_type`，而规则引擎（rule_engine）与评估函数
  期望的字段名为 `type`。因此在 defect_processor.py 的 extract_defects 函数中，需要执行：
      for d in defects:
          d["type"] = d.pop("defect_type", d.get("type"))
  以确保下游处理不受影响。此映射已纳入 defect_processor 最佳实践。
"""

import re
import json
import logging
from functools import lru_cache
from typing import List, Optional, Dict, Union

from crewai import Agent, Task, Crew, Process, LLM
from pydantic import ValidationError, BaseModel
from json_repair import repair_json

from app.core.config import settings
from app.core.exceptions import (
    FMEABaseException,
    LLMTimeoutError,
    LLMAPIError,
    ParsingError,
)
# 从 schemas 导入提取阶段的输出模型（已包含新字段）
from app.schemas import DefectExtractionResult, DefectBase

# -------------------------------------------------------------------
# 内部 FMEA 分析模型（含 AP 与 review_required，独立于 API 模型）
# -------------------------------------------------------------------
class FMEAItem(BaseModel):
    """FMEA 分析单条缺陷的输出模型"""
    id: int
    defect_type: str
    original_text: str
    severity: int
    occurrence: int
    detection: int
    rpn: int
    risk_level: str
    reasons: str = ""
    suggestion: str = ""
    # 新增字段
    ap: Optional[str] = None           # H / M / L
    review_required: bool = False      # 高风险闸门标记

class FMEAAnalysisResult(BaseModel):
    """完整的 FMEA 分析结果"""
    defects: List[FMEAItem]
    summary: str = ""

logger = logging.getLogger("defect_fmea.crews")


# ══════════════════════════════════════════════════
# LLM 单例（线程安全）
# ══════════════════════════════════════════════════
@lru_cache(maxsize=1)
def get_llm() -> LLM:
    """创建或获取共享的 LLM 实例，配置由 settings 统一提供。"""
    model = settings.LLM_MODEL
    base_url = settings.LLM_BASE_URL
    api_key = settings.LLM_API_KEY
    temperature = settings.LLM_TEMPERATURE
    max_tokens = settings.LLM_MAX_TOKENS

    if not api_key:
        raise LLMAPIError(
            "LLM_API_KEY 未设置，请在 .env 中配置有效的 API Key"
        )

    logger.info(
        "初始化 LLM | model=%s | base_url=%s | temperature=%s | max_tokens=%s",
        model, base_url, temperature, max_tokens,
    )
    instance = LLM(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return instance


# ══════════════════════════════════════════════════
# 通用工具：稳健 JSON 提取（兜底策略）
# ══════════════════════════════════════════════════
def extract_json_robust(raw_text: str) -> dict:
    """
    多级 JSON 提取/修复，确保返回可用字典。
    当 output_pydantic 机制失效时，本函数作为最后防线。
    """
    # 1. 已是字典
    if isinstance(raw_text, dict):
        return raw_text

    # 2. 直接解析
    try:
        return json.loads(raw_text)
    except Exception:
        pass

    # 3. 去除 Markdown 代码块标记后解析
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw_text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(cleaned[start:end + 1])
        except Exception:
            pass

    # 4. json_repair 全局修复
    try:
        repaired = repair_json(raw_text, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
        if isinstance(repaired, list):
            return {"defects": repaired}
    except Exception:
        pass

    # 5. 正则捕获最大花括号块 + json_repair
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if match:
        candidate = match.group()
        try:
            return json.loads(candidate)
        except Exception:
            try:
                r = repair_json(candidate, return_objects=True)
                if isinstance(r, dict):
                    return r
            except Exception:
                pass

    # 所有策略均失败
    logger.warning("⚠️ 所有 JSON 解析/修复步骤均失败，返回原始文本")
    return {"raw_output": raw_text, "parse_error": "无法解析为JSON"}


# ══════════════════════════════════════════════════
# 异常分类辅助
# ══════════════════════════════════════════════════
def _classify_llm_exception(e: Exception) -> FMEABaseException:
    """将通用异常归类为 FMEA 业务异常，便于上层统一处理。"""
    msg = str(e).lower()
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return LLMTimeoutError(f"LLM 调用超时: {e}")
    if "api key" in msg or "authentication" in msg or "unauthorized" in msg:
        return LLMAPIError(f"LLM 认证失败: {e}")
    if "rate limit" in msg or "quota" in msg:
        return LLMAPIError(f"LLM 额度或频率限制: {e}")
    return FMEABaseException(f"评估流程异常: {e}", error_code="EVALUATION_FAILED", status_code=500)


# ══════════════════════════════════════════════════
# 物理属性预评分引擎（定量规则）→ 已扩展 M‑2 逻辑
# ══════════════════════════════════════════════════
def _calc_severity(defect_type: str, depth: Optional[float], wall_thickness: Optional[float]) -> int:
    """严重度 S（1-10），壁厚/深度比决定基础分，裂纹类附加惩罚"""
    if wall_thickness and depth and wall_thickness > 0:
        ratio = depth / wall_thickness
        if ratio >= 0.8:
            base = 10
        elif ratio >= 0.5:
            base = 8
        elif ratio >= 0.3:
            base = 6
        elif ratio >= 0.1:
            base = 4
        else:
            base = 2
    else:
        base = 5   # 无尺寸信息时保守取值

    if defect_type and "裂纹" in defect_type:
        base = min(10, base + 2)
    return base


def _calc_occurrence(defect_type: str, quantity: Optional[int]) -> int:
    """发生度 O（1-10），数量越多可能性越高，腐蚀类易复发"""
    qty = quantity or 1
    if qty <= 1:
        o = 3
    elif qty <= 5:
        o = 5
    elif qty <= 10:
        o = 7
    else:
        o = 9

    if defect_type and "腐蚀" in defect_type:
        o = min(10, o + 2)
    return o


def _calc_detection(depth: Optional[float]) -> int:
    """检测度 D（1-10），尺寸越小越难发现"""
    if depth is None:
        return 7
    if depth >= 5:
        return 2
    elif depth >= 2:
        return 4
    elif depth >= 1:
        return 7
    else:
        return 9


def pre_score_defects(defects: List[Dict]) -> List[Dict]:
    """
    对缺陷列表执行物理规则预评分，为每个缺陷增加：
    severity, occurrence, detection, pre_rpn
    并应用 M‑2 扩展调整（服役年限、检验间隔、检测方法）
    """
    scored = []
    for d in defects:
        s = _calc_severity(d.get("defect_type", ""), d.get("depth"), d.get("wall_thickness"))
        o = _calc_occurrence(d.get("defect_type", ""), d.get("quantity"))
        det = _calc_detection(d.get("depth"))

        # ---------- M‑2 新增微调逻辑 ----------
        # 1. 设备老化且未定期检验 → 潜在风险上升
        service_years = d.get("service_years")
        if service_years and service_years > 20 and not d.get("inspection_interval"):
            s = min(10, s + 2)

        # 2. 手动检测方法 → 检出难度增加
        method = d.get("detection_method", "")
        if method and ("manual" in str(method).lower() or "手动" in str(method)):
            det = min(10, det + 2)

        d["severity"] = s
        d["occurrence"] = o
        d["detection"] = det
        d["pre_rpn"] = s * o * det
        scored.append(d)
    return scored


# ══════════════════════════════════════════════════
# AP 行动优先级计算（M‑4）
# ══════════════════════════════════════════════════
def calculate_ap(severity: int, occurrence: int, detection: int) -> str:
    """
    简化版 AP 行动优先级（参照 VDA/AIAG 表格）
    H - 高优先级，需立即措施
    M - 中优先级，需计划措施
    L - 低优先级，可接受风险
    """
    if severity >= 9 or (severity >= 5 and occurrence >= 4):
        return "H"
    elif severity >= 5 or occurrence >= 3 or detection >= 7:
        return "M"
    else:
        return "L"


# ══════════════════════════════════════════════════
# 第一阶段：缺陷提取 Crew（强化 Prompt 版本）
# ══════════════════════════════════════════════════
def extract_defects(report_text: str) -> DefectExtractionResult:
    """
    从非结构化检验报告中提取缺陷，返回 Pydantic 对象。

    流程：
    1. 调用 LLM 输出纯 JSON 文本（不依赖 response_format）。
    2. 使用 extract_json_robust 解析并修复 JSON。
    3. 通过 DefectExtractionResult.model_validate 强校验结构（自动映射字段、补充缺失值）。

    Returns:
        DefectExtractionResult 包含 defects 列表，每个缺陷包含壁厚、深度、新字段等
    """
    agent = Agent(
        role="特种设备缺陷解析专家",
        goal="从检验报告中精确提取所有缺陷，输出纯 JSON。若报告无明确设计壁厚，wall_thickness 设为 null，严禁猜测。",
        backstory="你只负责客观提取数据，不添加任何分析或解释。",
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )

    # 强化后的 Prompt：明确字段名、示例结构、关键约束
    task = Task(
        description=f"""请根据以下报告文本，提取所有缺陷并严格按照 JSON Schema 输出。

报告文本：
{report_text}

⚠️ 关键约束：
- 输出必须是合法的 JSON 对象，不要用 Markdown 代码块包裹（即不要出现 ```json 标记）。
- 必须使用以下字段名称，**不允许省略任何字段**：
    · defect_type      → 缺陷类型（如“裂纹”、“腐蚀”）
    · original_text    → 原始报告中该缺陷所在的原文片段（一句即可，必须提供）
    · location         → 缺陷位置（如“筒体焊缝”）
    · depth            → 深度（mm），数值，若无则为 null
    · length           → 长度（mm），数值，若无则为 null
    · wall_thickness   → 设计壁厚（mm），如报告中未明确给出，必须设为 null
    · detection_method → 检测方法（如“手动超声”），若无则为 null
    · service_years    → 设备服役年限（数字），若无则为 null
    · inspection_interval → 检验间隔（如“2年”），若无则为 null
    · quantity         → 数量（整数），默认 1
    · component        → 部件（如“筒体”、“焊缝”），若无则为 null
    · unit             → 单位，默认 "mm"

- 请严格按照以下示例格式输出（注意字段名必须完全一致）：
  {{
    "defects": [
      {{
        "defect_type": "裂纹",
        "original_text": "筒体焊缝存在长度约120mm的裂纹",
        "location": "筒体焊缝",
        "depth": null,
        "length": 120,
        "wall_thickness": null,
        "detection_method": null,
        "service_years": null,
        "inspection_interval": null,
        "quantity": 1,
        "component": null,
        "unit": "mm"
      }}
    ]
  }}

只输出 JSON 对象，不要包含任何解释。""",
        expected_output="一个纯 JSON 对象，不包含任何额外字符",
        agent=agent,
        # 已移除 output_pydantic，避免 DeepSeek 不支持的 response_format
    )

    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )

    try:
        result = crew.kickoff()
    except FMEABaseException:
        raise
    except Exception as e:
        logger.error(f"缺陷提取任务执行失败: {e}")
        raise _classify_llm_exception(e) from e

    # 获取原始输出文本
    raw = result.raw if hasattr(result, "raw") else str(result)
    logger.debug("LLM 原始输出（前300字符）: %s", raw[:300])

    # 统一使用 robust 解析，处理各类格式异常
    parsed = extract_json_robust(raw)
    try:
        # 使用 model_validate 进行严格的 Pydantic 校验（会自动调用 model_validator 完成字段映射与默认值填充）
        return DefectExtractionResult.model_validate(parsed)
    except ValidationError as e:
        raise ParsingError(
            f"LLM 输出不符合缺陷提取 schema。错误: {e}\n原始输出片段: {raw[:300]}"
        ) from e
    except Exception as e:
        raise ParsingError(f"提取结果解析失败: {e}") from e


# ══════════════════════════════════════════════════
# 第二阶段：FMEA 分析 Crew（集成高风险闸门 + AP）
# ══════════════════════════════════════════════════
def analyze_fmea(defects: Union[DefectExtractionResult, List[Dict]]) -> FMEAAnalysisResult:
    """
    对缺陷列表执行 FMEA 分析：
        1. 物理属性预评分（含 M‑2 调整）
        2. LLM 结合规则与工程经验微调
        3. 自动计算 AP 行动优先级
        4. 根据高风险闸门标记 review_required
        5. 输出最终 FMEAAnalysisResult

    Args:
        defects: 可以是提取阶段的 DefectExtractionResult 对象，或已转为字典的缺陷列表

    Returns:
        FMEAAnalysisResult 包含每条缺陷的完整 FMEA 评估
    """
    # 统一转换为字典列表
    if isinstance(defects, DefectExtractionResult):
        raw_list = defects.model_dump().get("defects", [])
    elif isinstance(defects, list):
        raw_list = defects
    else:
        raise ValueError("defects 参数类型错误，需要 DefectExtractionResult 或 list[dict]")

    if not raw_list:
        raise ParsingError("缺陷列表为空，无法进行 FMEA 分析", status_code=400)

    # 1. 定量预评分（已包含 M‑2 调整）
    scored = pre_score_defects(raw_list)

    # 2. 构建 LLM 提示词（注入预评分与基础规则）
    defect_lines = []
    for idx, d in enumerate(scored, start=1):
        line = (
            f"缺陷 {idx}: "
            f"类型={d.get('defect_type', '未知')}, "
            f"壁厚={d.get('wall_thickness')}, 深度={d.get('depth')}mm, "
            f"长度={d.get('length')}mm, 数量={d.get('quantity', 1)}, "
            f"检测方法={d.get('detection_method', '未知')}, "
            f"服役年限={d.get('service_years', '未知')}年, "
            f"检验间隔={d.get('inspection_interval', '未知')}, "
            f"系统预评 S={d['severity']} O={d['occurrence']} D={d['detection']} (预RPN={d['pre_rpn']}), "
            f"原始描述: {d.get('original_text', '')[:80]}..."
        )
        defect_lines.append(line)

    prompt = f"""
作为承压设备 FMEA 评审专家，请根据以下缺陷信息及系统预评分，输出最终评估结果（JSON 数组）。
系统预评分已参考设备服役年限、检验间隔、检测方法等因素进行了调整，你可在其基础上结合工程经验微调（幅度 ≤2分），并说明理由。

严格遵循以下规则：
1. S（严重度）主要参考“深度/壁厚”比，裂纹、贯穿性缺陷可+2（上限10）。
2. O（发生度）参考数量和历史，腐蚀类缺陷易复发可+1~2。
3. D（检测度）参考尺寸，小于1mm深度极难发现，可设为8-10。
4. 风险等级按 RPN 划分：低（1-50）、中（51-100）、高（101-200）、严重（>200）。
5. 对于高严重度或高 RPN 的缺陷，务必给出明确改进建议。

具体缺陷清单：
{chr(10).join(defect_lines)}

输出格式（严格 JSON 数组，无注释，不要包含 Markdown 代码块）：
[
  {{
    "id": 1,
    "defect_type": "...",
    "original_text": "...",
    "severity": <int>,
    "occurrence": <int>,
    "detection": <int>,
    "rpn": <自动计算>,
    "risk_level": "低/中/高/严重",
    "reasons": "调整理由...",
    "suggestion": "改进建议..."
  }},
  ...
]
同时，在数组外加一个 "summary" 字段（可选），对整体情况作简短总结。
"""

    agent = Agent(
        role="FMEA 风险评估专家",
        goal="结合工程经验与规则，对预评分进行专家级微调，输出可靠的风险评估。",
        backstory="你精通 TSG21、ASME 等标准，能精准判断缺陷风险。",
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )

    task = Task(
        description=prompt,
        expected_output="符合描述格式的 JSON 数组，包含 summary 字段",
        agent=agent,
    )

    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )

    try:
        raw_result = crew.kickoff()
    except FMEABaseException:
        raise
    except Exception as e:
        logger.error(f"FMEA 分析任务执行失败: {e}")
        raise _classify_llm_exception(e) from e

    # 解析 LLM 输出
    raw_text = raw_result.raw if hasattr(raw_result, "raw") else str(raw_result)
    analysis_dict = extract_json_robust(raw_text)

    # 可能 LLM 直接返回了数组，需要包装成 {"defects": ..., "summary": "..."}
    if isinstance(analysis_dict, list):
        analysis_dict = {"defects": analysis_dict, "summary": ""}
    if "defects" not in analysis_dict and isinstance(analysis_dict, dict):
        analysis_dict = {"defects": [analysis_dict]}

    # 提取缺陷列表与 summary
    raw_defects = analysis_dict.get("defects", [])
    summary = analysis_dict.get("summary", "")

    # 为每条缺陷注入 AP 和 review_required
    result_items = []
    for item_dict in raw_defects:
        try:
            item = FMEAItem(**item_dict)
        except ValidationError as e:
            logger.warning(f"跳过格式错误的缺陷项: {e}")
            continue

        # 计算 AP
        item.ap = calculate_ap(item.severity, item.occurrence, item.detection)

        # 高风险闸门（M‑3）
        if item.rpn >= settings.HIGH_RISK_THRESHOLD:
            item.review_required = True
        elif settings.FORCE_SUSPEND_S9 and item.severity >= 9:
            item.review_required = True
        else:
            item.review_required = False

        result_items.append(item)

    if not result_items:
        raise ParsingError("FMEA 分析后未生成任何有效缺陷记录")

    result_obj = FMEAAnalysisResult(defects=result_items, summary=summary)
    logger.info(
        f"✅ FMEA 分析完成 | 缺陷总数={len(result_items)} | "
        f"需审核={sum(1 for i in result_items if i.review_required)} | "
        f"高AP={sum(1 for i in result_items if i.ap == 'H')}"
    )
    return result_obj


# ══════════════════════════════════════════════════
# 端到端快捷接口（提取 + 分析）
# ══════════════════════════════════════════════════
def run_full_fmea(report_text: str) -> FMEAAnalysisResult:
    """
    完整的 FMEA 流程：从检验报告文本直接输出最终 FMEA 评估。

    Returns:
        FMEAAnalysisResult 包含每条缺陷的详细评估（含 AP 与审核标记）。
    """
    logger.info("🚀 开始全流程 FMEA：缺陷提取 → 预评分 → 专家评审")
    defects = extract_defects(report_text)
    return analyze_fmea(defects)


# ══════════════════════════════════════════════════
# 向后兼容：保留旧版函数名及 API
# ══════════════════════════════════════════════════
create_analysis_crew = extract_defects  # 旧版名称映射


def run_fmea_evaluation(text: str) -> dict:
    """
    [已废弃] 旧版 FMEA 评估函数，返回纯字典。
    推荐使用新接口 `run_full_fmea`。
    """
    logger.warning("调用了已废弃的 run_fmea_evaluation，建议迁移到 run_full_fmea")
    result = run_full_fmea(text)
    return result.model_dump()


def run_full_fmea_evaluation(text: str) -> dict:
    """
    [已废弃] 旧版完整评估函数，返回纯字典。
    推荐使用新接口 `run_full_fmea`。
    """
    logger.warning("调用了已废弃的 run_full_fmea_evaluation，建议迁移到 run_full_fmea")
    return run_full_fmea(text).model_dump()