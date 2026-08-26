"""
app/crews.py – 缺陷提取与 FMEA 分析双智能体（终极版 v8.3）

核心功能：
1. 缺陷提取（稳健 JSON 解析，兼容不支持 response_format 的 LLM）
2. 物理属性预评分（壁厚/深度/数量 + 服役年限/检测方法）
3. FMEA 分析（LLM 精修预评分，输出 RPN / 风险等级 / 建议）【已不用于主流程】
4. AP 行动优先级自动计算（M‑4）
5. 高风险闸门（M‑3）：自动标记需审核的缺陷
6. 端到端流水线（run_full_fmea）-- 统一同步/异步评级路径，走规则引擎
7. 向后兼容旧版 API（run_fmea_evaluation / run_full_fmea_evaluation）

v8.3 修复说明（本次）：
- wall_thickness 提取修复：Prompt 中字段定义改为「壁厚/设计壁厚」，
  明确缺陷描述中的「壁厚XXmm」也应提取；新增 _propagate_wall_thickness()
  在提取后对唯一壁厚值做自动传播，双保险避免全部丢失。
- 增加设备级信息适用性说明（头部字段适用于所有缺陷），锁定正确行为。
- get_llm() 增加 timeout 参数（推理模型 300s / 非推理 120s，可被 LLM_TIMEOUT
  覆盖），带 try/except 兼容降级，避免旧版 CrewAI 不支持时崩溃。

v8.2.1 历史修复说明（保留）：
- 移除 model_kwargs 传递：CrewAI 1.15.x 的 LLM 类不支持该参数，
  会将其透传给 OpenAI SDK 导致 Completions.create() 报错。

v8.2 历史修改说明（保留）：
- 增强 get_llm()：识别推理模型（如 deepseek-reasoner / v4-pro / r1 / o1 等），
  自动提升 max_tokens 至 16000 以上，为思考过程预留充足空间。
- 新增 _safe_raw_text()：从 Crew 结果中安全提取原始输出文本，
  同时兼容 content / reasoning_content 字段，避免空响应导致解析崩溃。
- 新增 _is_empty_llm_response()：检测 LLM 空响应并给出针对性业务异常。
- extract_defects() 与 analyze_fmea() 增加空响应防御和兜底返回。

v8.1 历史修改说明（保留）：
- 将 run_full_fmea 的主流程改为：LLM 提取缺陷 → 规则引擎评级（evaluate_one_defect）
  → 规则引擎审计（audit_one_defect），与异步任务完全一致，确保可解释性。
- 统一风险等级为四级体系：低风险（RPN≤50）、中风险（51~100）、高风险（101~200）、极高风险（>200）。
- 新增 _rpn_to_risk_level 工具函数，在缺陷项缺省 risk_level 时根据 RPN 自动补全。
"""

import re
import json
import logging
import time
from functools import lru_cache
from typing import List, Optional, Dict, Union, Any

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

# v8.1 新增：导入规则引擎的评估与审计函数，确保同步/异步一致
from app.core.defect_processor import evaluate_one_defect, audit_one_defect

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
    risk_level: str              # 四级：低风险/中风险/高风险/极高风险
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
# 推理模型识别
# ══════════════════════════════════════════════════
# 推理模型名称关键词（小写匹配）
REASONING_MODEL_KEYWORDS = (
    "reasoner", "r1", "o1", "o3", "thinking", "pro",
)

# 非推理模型的 max_tokens 最低保证
NON_REASONING_MIN_TOKENS = 8000
# 推理模型的 max_tokens 最低保证（思考 + 正文）
REASONING_MIN_TOKENS = 16000

# 超时默认值（秒），可被 settings.LLM_TIMEOUT 覆盖
DEFAULT_TIMEOUT_REASONING = 300.0
DEFAULT_TIMEOUT_NON_REASONING = 120.0


def is_reasoning_model(model_name: str) -> bool:
    """
    判断模型是否为推理增强型（如 deepseek-reasoner、deepseek-v4-pro）。
    """
    if not model_name:
        return False
    lowered = model_name.lower()
    return any(kw in lowered for kw in REASONING_MODEL_KEYWORDS)


# ══════════════════════════════════════════════════
# LLM 单例（线程安全）
# ══════════════════════════════════════════════════
@lru_cache(maxsize=1)
def get_llm() -> LLM:
    """
    创建或获取共享的 LLM 实例，配置由 settings 统一提供。

    增强功能：
      - 自动识别推理模型并大幅提升 max_tokens（≥16000）。
      - 推理模型自动降低温度至 0.2 以下，提高 JSON 输出稳定性。
      - 非推理模型最低保证 max_tokens ≥ 8000。
      - 增加 timeout 超时控制（v8.3），推理模型默认 300s，非推理 120s。

    v8.2.1 修复：
      - 移除 model_kwargs 参数，避免 CrewAI 将其透传给 OpenAI SDK 导致错误。
      - LLM_ENABLE_THINKING=False 时仅记录警告（该版本 CrewAI 无法安全关闭推理）。
    """
    model = settings.LLM_MODEL
    base_url = settings.LLM_BASE_URL
    api_key = settings.LLM_API_KEY
    temperature = settings.LLM_TEMPERATURE
    max_tokens = settings.LLM_MAX_TOKENS

    reasoning = is_reasoning_model(model)

    # ── 1. 根据模型类型确定最低输出预算 ──
    if reasoning:
        if max_tokens < REASONING_MIN_TOKENS:
            logger.warning(
                "⚠️ 检测到推理模型 %s，LLM_MAX_TOKENS(%s) 过低，"
                "思考过程可能耗尽全部额度导致空响应。自动提升至 %s。",
                model, max_tokens, REASONING_MIN_TOKENS,
            )
            max_tokens = REASONING_MIN_TOKENS
        # 推理模型建议使用更低的温度以保证结构化输出稳定性
        if temperature > 0.2:
            logger.info("🌡️ 推理模型 %s 温度由 %.2f 调整为 0.2", model, temperature)
            temperature = 0.2
    else:
        if max_tokens < NON_REASONING_MIN_TOKENS:
            logger.warning(
                "⚠️ LLM_MAX_TOKENS=%s 可能导致输出截断；自动提升至 %s。"
                "建议在 .env 中设置 LLM_MAX_TOKENS=%s。",
                max_tokens, NON_REASONING_MIN_TOKENS, NON_REASONING_MIN_TOKENS,
            )
            max_tokens = NON_REASONING_MIN_TOKENS

    if not api_key:
        raise LLMAPIError(
            "LLM_API_KEY 未设置，请在 .env 中配置有效的 API Key"
        )

    # ── 2. 超时配置（v8.3 新增）──
    timeout = getattr(settings, "LLM_TIMEOUT", None)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_REASONING if reasoning else DEFAULT_TIMEOUT_NON_REASONING
    else:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            logger.warning("⚠️ LLM_TIMEOUT 配置值非法，使用默认值")
            timeout = DEFAULT_TIMEOUT_REASONING if reasoning else DEFAULT_TIMEOUT_NON_REASONING

    # ── 3. 提示 LLM_ENABLE_THINKING 的不可用性（仅记录警告）──
    if reasoning and not settings.LLM_ENABLE_THINKING:
        logger.warning(
            "🔇 检测到 LLM_ENABLE_THINKING=False，但当前 CrewAI 版本"
            "无法安全传递 thinking 控制参数，已忽略该设置。"
            "建议改用非推理模型（如 deepseek-chat）以彻底避免推理开销。"
        )

    logger.info(
        "初始化 LLM | model=%s | reasoning=%s | base_url=%s | "
        "temperature=%s | max_tokens=%s | timeout=%ss",
        model, reasoning, base_url, temperature, max_tokens, timeout,
    )

    # ✅ v8.2.1：不再传递 model_kwargs
    # ✅ v8.3：尝试传递 timeout，旧版 CrewAI 不支持时优雅降级
    try:
        instance = LLM(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except TypeError as e:
        logger.warning(
            "⚠️ 当前 CrewAI 版本不支持 timeout 参数（%s），已忽略超时配置", e
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
# 安全提取 Crew 原始输出
# ══════════════════════════════════════════════════
def _safe_raw_text(result: Any) -> str:
    """
    从 Crew 执行结果中安全提取原始文本。

    CrewAI 不同版本中，结果可能包含：
      - result.raw           → 标准聚合输出字符串
      - result.content       → 最新任务的内容
      - result.reasoning     → 推理过程文本（罕见）
      - str(result)          → 对象的字符串表示

    本函数按优先级依次尝试，并对 None / 空串做归一化处理。
    """
    # 1. 优先尝试 .raw
    raw = getattr(result, "raw", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    # 2. 尝试 .content
    content = getattr(result, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()

    # 3. 尝试 .reasoning_content（某些推理模型可能将正文放在此字段）
    reasoning_content = getattr(result, "reasoning_content", None)
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        logger.warning("⚠️ LLM 返回的正文位于 reasoning_content 字段（非常规）")
        return reasoning_content.strip()

    # 4. 兜底：字符串表示
    text = str(result).strip()
    if text:
        return text

    # 5. 完全为空
    return ""


def _is_empty_llm_response(raw: str) -> bool:
    """判断 LLM 输出是否为空（排除仅含空白字符的情况）。"""
    return not raw or not raw.strip()


def _raise_for_empty_response(raw: str) -> None:
    """
    当 LLM 返回空内容时，抛出明确的业务异常，
    帮助用户定位是 max_tokens 不足还是平台限制。
    """
    if _is_empty_llm_response(raw):
        raise LLMAPIError(
            "LLM 返回了空响应。若当前使用推理模型（如 deepseek-v4-pro），"
            "大概率是因为 max_tokens 不足，思考过程耗尽全部额度导致无正文输出。"
            f"当前配置: LLM_MODEL={settings.LLM_MODEL}, "
            f"LLM_MAX_TOKENS={settings.LLM_MAX_TOKENS}。"
            "建议：1) 增加 LLM_MAX_TOKENS 至 16000 以上；"
            "2) 或改用非推理模型（如 deepseek-chat）。"
        )


# ══════════════════════════════════════════════════
# v8.3 新增：壁厚自动传播
# ══════════════════════════════════════════════════
def _propagate_wall_thickness(defects: List[Dict]) -> List[Dict]:
    """
    壁厚自动传播（v8.3）。

    当多条缺陷中仅存在一个唯一有效壁厚值时，通常该值是整台设备的
    公称壁厚，将其传播到其他缺失壁厚的缺陷。
    若存在多个不同的壁厚值（例如不同部件），则保守不传播。

    注：本函数是提取层的兜底；下游任务层也有独立的传播逻辑。
    """
    if not defects:
        return defects

    values = {
        d.get("wall_thickness")
        for d in defects
        if d.get("wall_thickness") is not None
    }

    # 仅当有且只有一个唯一壁厚值时传播
    if len(values) != 1:
        return defects

    target = values.pop()
    propagated_count = 0
    for d in defects:
        if d.get("wall_thickness") is None:
            d["wall_thickness"] = target
            propagated_count += 1

    if propagated_count:
        logger.info(
            "🔄 壁厚自动传播：唯一有效壁厚 %s，已传播至 %d 条缺陷",
            target, propagated_count,
        )

    return defects


# ══════════════════════════════════════════════════
# 通用工具：稳健 JSON 提取（兜底策略）
# ══════════════════════════════════════════════════
def extract_json_robust(raw_text: str) -> dict:
    """
    多级 JSON 提取/修复，确保返回可用字典。
    当 output_pydantic 机制失效时，本函数作为最后防线。
    """
    # 防御：None 或非字符串输入
    if raw_text is None:
        return {"raw_output": "", "parse_error": "输入为空"}
    if isinstance(raw_text, dict):
        return raw_text
    if not isinstance(raw_text, str):
        raw_text = str(raw_text)

    # 1. 直接解析
    try:
        return json.loads(raw_text)
    except Exception:
        pass

    # 2. 去除 Markdown 代码块标记后解析
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw_text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(cleaned[start:end + 1])
        except Exception:
            pass

    # 3. json_repair 全局修复
    try:
        repaired = repair_json(raw_text, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
        if isinstance(repaired, list):
            return {"defects": repaired}
    except Exception:
        pass

    # 4. 正则捕获最大花括号块 + json_repair
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
    if "empty" in msg or "none" in msg:
        return LLMAPIError(f"LLM 返回空响应: {e}")
    return FMEABaseException(f"评估流程异常: {e}", error_code="EVALUATION_FAILED", status_code=500)


# ══════════════════════════════════════════════════
# 带重试的 Crew 执行封装
# ══════════════════════════════════════════════════
def _kickoff_with_retry(crew: Crew, label: str = "crew") -> Any:
    """
    执行 Crew 任务，并在失败/空响应时按配置自动重试（指数退避）。

    Args:
        crew: 已构建的 Crew 实例
        label: 日志标签（用于区分步骤）

    Returns:
        Crew 执行结果对象

    Raises:
        FMEABaseException: 重试耗尽后抛出明确的业务异常
    """
    max_retries = max(0, int(settings.LLM_MAX_RETRIES))
    delay = max(0.0, float(settings.LLM_RETRY_DELAY))

    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            result = crew.kickoff()

            # 检查结果是否为空响应
            raw = _safe_raw_text(result)
            if _is_empty_llm_response(raw):
                logger.warning(
                    "⚠️ [%s] 第 %d 次执行返回空响应。",
                    label, attempt + 1,
                )
                last_exc = LLMAPIError(
                    "LLM 返回空响应，疑似推理 token 耗尽或平台限制"
                )
                if attempt < max_retries:
                    wait = delay * (2 ** attempt)
                    logger.info("⏳ %s 秒后重试...", wait)
                    time.sleep(wait)
                    continue
                # 重试耗尽
                raise last_exc

            return result

        except FMEABaseException:
            # 业务异常直接向上抛，不重试
            raise
        except Exception as e:
            last_exc = e
            logger.error(
                "❌ [%s] 第 %d 次执行失败: %s", label, attempt + 1, e
            )
            if attempt < max_retries:
                wait = delay * (2 ** attempt)
                logger.info("⏳ %s 秒后自动重试...", wait)
                time.sleep(wait)
            else:
                logger.error("💀 [%s] 重试 %d 次后仍失败", label, max_retries)

    # 重试耗尽，抛出归类后的业务异常
    raise _classify_llm_exception(last_exc) if last_exc else LLMAPIError(
        f"[{label}] 执行失败且无具体错误信息"
    )


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


def _rpn_to_risk_level(rpn: int) -> str:
    """根据 RPN 值返回统一的风险等级文字（四级体系）"""
    if rpn > 200:
        return "极高风险"
    elif rpn > 100:
        return "高风险"
    elif rpn > 50:
        return "中风险"
    else:
        return "低风险"


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
# 第一阶段：缺陷提取 Crew（强化 Prompt + v8.3 壁厚修复）
# ══════════════════════════════════════════════════
def extract_defects(report_text: str) -> DefectExtractionResult:
    """
    从非结构化检验报告中提取缺陷，返回 Pydantic 对象。

    流程：
    1. 调用 LLM 输出纯 JSON 文本（不依赖 response_format）。
    2. 使用 _safe_raw_text 安全提取原始输出（防御空响应）。
    3. 使用 extract_json_robust 解析并修复 JSON。
    4. v8.3 新增：壁厚自动传播（_propagate_wall_thickness）。
    5. 通过 DefectExtractionResult.model_validate 强校验结构。

    Returns:
        DefectExtractionResult 包含 defects 列表

    Raises:
        LLMAPIError: LLM 返回空响应或调用失败
        ParsingError: 输出格式异常无法解析
    """
    agent = Agent(
        role="特种设备缺陷解析专家",
        goal="从检验报告中精确提取所有缺陷，输出纯 JSON。若报告无明确壁厚，wall_thickness 设为 null，严禁猜测。",
        backstory="你只负责客观提取数据，不添加任何分析或解释。",
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )

    # v8.3 强化后的 Prompt：修复 wall_thickness 定义 + 设备级信息适用性说明
    task = Task(
        description=f"""请根据以下报告文本，提取所有缺陷并严格按照 JSON Schema 输出。

报告文本：
{report_text}

⚠️ 关键约束：
- 输出必须是合法的 JSON 对象，不要用 Markdown 代码块包裹（即不要出现 ```json 标记）。
- 报告头部给出的设备级信息（介质、材质、设备类别、设计压力、服役年限、检测方法）适用于所有缺陷，应填入每条缺陷记录；若头部未给出则为 null。
- 必须使用以下字段名称，**不允许省略任何字段**：
    · defect_type      → 缺陷类型（如“裂纹”、“腐蚀”）
    · original_text    → 原始报告中该缺陷所在的原文片段（一句即可，必须提供）
    · location         → 缺陷位置（如“筒体焊缝”）
    · depth            → 深度（mm），数值，若无则为 null
    · length           → 长度（mm），数值，若无则为 null
    · wall_thickness   → 壁厚/设计壁厚（mm）。若报告任何位置出现“壁厚XXmm”或“设计壁厚XXmm”（包括缺陷描述中的参考壁厚），应直接取该数值；仅当报告中完全未出现任何壁厚数值时才设为 null，严禁自行猜测
    · detection_method → 检测方法（如“手动超声”），若无则为 null
    · service_years    → 设备服役年限（数字），若无则为 null
    · inspection_interval → 检验间隔（如“2年”），若无则为 null
    · quantity         → 数量（整数），默认 1
    · component        → 部件（如“筒体”、“焊缝”），若无则为 null
    · unit             → 单位，默认 "mm"
    · media            → 充装/接触介质（如“液氨”、“氯”），若报告未提及则为 null
    · material         → 罐体/构件材质（如“Q345R”），若报告未提及则为 null
    · device_type      → 设备类型/大类（如“移动式压力容器”），若报告未提及则为 null
    · environment      → 使用环境描述（如“室外”、“海洋大气”），若报告未提及则为 null
    · operating_temperature → 操作温度（℃，数值），若报告未提及则为 null
    · design_pressure  → 设计压力（MPa，数值），若报告未提及则为 null

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
        "unit": "mm",
        "media": "液氨",
        "material": "Q345R",
        "device_type": "移动式压力容器",
        "environment": "室外",
        "operating_temperature": null,
        "design_pressure": 2.5
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

    # 使用带重试和空响应检测的执行封装
    result = _kickoff_with_retry(crew, label="缺陷提取")

    # 获取原始输出文本（安全提取）
    raw = _safe_raw_text(result)

    # 检测空响应，直接抛出明确异常
    _raise_for_empty_response(raw)

    logger.debug("LLM 原始输出（前300字符）: %s", raw[:300])

    # 统一使用 robust 解析，处理各类格式异常
    parsed = extract_json_robust(raw)

    # ── v8.3 新增：壁厚自动传播（兜底保护）──
    if isinstance(parsed, dict) and isinstance(parsed.get("defects"), list):
        parsed["defects"] = _propagate_wall_thickness(parsed["defects"])

    try:
        # 使用 model_validate 进行严格的 Pydantic 校验
        return DefectExtractionResult.model_validate(parsed)
    except ValidationError as e:
        raise ParsingError(
            f"LLM 输出不符合缺陷提取 schema。错误: {e}\n原始输出片段: {raw[:300]}"
        ) from e
    except Exception as e:
        raise ParsingError(f"提取结果解析失败: {e}") from e


# ══════════════════════════════════════════════════
# 第二阶段：FMEA 分析 Crew（仅用于实验对比，非主流程）
# ══════════════════════════════════════════════════
def analyze_fmea(defects: Union[DefectExtractionResult, List[Dict]]) -> FMEAAnalysisResult:
    """
    [保留用于实验对比] 对缺陷列表执行 LLM 精修 FMEA 分析。
    注意：主流程（同步与异步）已改用规则引擎，本函数不再被 run_full_fmea 调用。

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

    def _process_defect_item(item_dict: dict, fallback_id: int) -> Optional[FMEAItem]:
        """处理单个缺陷字典，补全缺失字段并构造 FMEAItem，失败返回 None。"""
        if not isinstance(item_dict, dict):
            return None
        # 补全缺失字段，避免因个别缺失而丢弃整条记录
        item_dict.setdefault("id", fallback_id)
        item_dict.setdefault("defect_type", "未知缺陷")
        item_dict.setdefault("original_text", "")
        item_dict.setdefault("severity", 5)
        item_dict.setdefault("occurrence", 3)
        item_dict.setdefault("detection", 4)
        item_dict.setdefault(
            "rpn",
            item_dict["severity"] * item_dict["occurrence"] * item_dict["detection"]
        )
        item_dict.setdefault("risk_level", _rpn_to_risk_level(item_dict["rpn"]))
        item_dict.setdefault("reasons", "")
        item_dict.setdefault("suggestion", "")
        try:
            return FMEAItem(**item_dict)
        except ValidationError as e:
            logger.warning(f"缺陷项格式错误，已跳过: {e}")
            return None

    # 2. 构建 LLM 提示词（注入预评分与基础规则，同时携带上下文信息）
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
            f"介质={d.get('media', '未知')}, 材质={d.get('material', '未知')}, "
            f"设备类型={d.get('device_type', '未知')}, 环境={d.get('environment', '未知')}, "
            f"操作温度={d.get('operating_temperature', '未知')}℃, "
            f"设计压力={d.get('design_pressure', '未知')}MPa, "
            f"系统预评 S={d['severity']} O={d['occurrence']} D={d['detection']} (预RPN={d['pre_rpn']}), "
            f"原始描述: {d.get('original_text', '')[:80]}..."
        )
        defect_lines.append(line)

    prompt = f"""
你是承压设备 FMEA 评审专家。请根据以下缺陷信息及系统预评分，输出最终评分(JSON 数组)。
你只需输出 JSON 数组，不要附加任何解释，不要输出 summary 字段。

规则：
1. S（严重度）: 参考“深度/壁厚”比，裂纹可+2（上限10）。
2. O（发生度）: 参考数量，腐蚀类可+1~2。
3. D（检测度）: 参考尺寸，深度<1mm 可设为8-10。
4. 风险等级按 RPN: 低(1-50)、中(51-100)、高(101-200)、极高(>200)。
5. 对高风险缺陷给出简洁建议。

具体缺陷清单：
{chr(10).join(defect_lines)}

输出格式（严格 JSON 数组，不要用 Markdown 代码块）：
[
  {{
    "id": 1,
    "defect_type": "...",
    "original_text": "...",
    "severity": <int>,
    "occurrence": <int>,
    "detection": <int>,
    "rpn": <自动计算>,
    "risk_level": "低/中/高/极高",
    "reasons": "简短调整理由（不超过20字）",
    "suggestion": "简短改进建议（不超过30字）"
  }}
]
只输出一个 JSON 数组，不要包含任何其他内容。
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
        expected_output="符合描述格式的 JSON 数组",
        agent=agent,
    )

    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )

    # 带重试与空响应检测
    raw_result = _kickoff_with_retry(crew, label="FMEA分析")

    # 解析 LLM 输出
    raw_text = _safe_raw_text(raw_result)
    _raise_for_empty_response(raw_text)

    analysis_dict = extract_json_robust(raw_text)

    # 可能 LLM 直接返回了数组，需要包装成 {"defects": ..., "summary": "..."}
    if isinstance(analysis_dict, list):
        analysis_dict = {"defects": analysis_dict, "summary": ""}
    if not isinstance(analysis_dict, dict):
        logger.error("FMEA 分析输出无法解析为字典，内容: %s", raw_text[:500])
        analysis_dict = {"defects": [], "summary": ""}
    if "defects" not in analysis_dict and isinstance(analysis_dict, dict):
        # 可能 LLM 只返回了一个缺陷对象
        analysis_dict = {"defects": [analysis_dict]}

    raw_defects = analysis_dict.get("defects", [])
    summary = analysis_dict.get("summary", "")

    # 为每条缺陷注入 AP 和 review_required
    result_items = []
    for idx, item_dict in enumerate(raw_defects, start=1):
        item = _process_defect_item(item_dict, idx)
        if item is None:
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

    # 批量解析失败时，自动降级为单条 LLM 评估
    if not result_items:
        logger.warning("批量 FMEA 解析无有效结果，自动降级为逐条评估...")
        for idx, d in enumerate(scored, start=1):
            # 为单条缺陷构建简化提示词
            single_defect_lines = [defect_lines[idx-1]]
            single_prompt = f"""
你是承压设备 FMEA 评审专家。请根据以下单条缺陷信息及系统预评分，输出最终评分(JSON 对象)。
你只需输出一个 JSON 对象，不要附加任何解释。

规则：
1. S（严重度）: 参考“深度/壁厚”比，裂纹可+2（上限10）。
2. O（发生度）: 参考数量，腐蚀类可+1~2。
3. D（检测度）: 参考尺寸，深度<1mm 可设为8-10。
4. 风险等级按 RPN: 低(1-50)、中(51-100)、高(101-200)、极高(>200)。
5. 对高风险缺陷给出简洁建议。

缺陷信息：
{single_defect_lines[0]}

输出格式（严格 JSON 对象，不要用 Markdown 代码块）：
{{
  "id": {idx},
  "defect_type": "...",
  "original_text": "...",
  "severity": <int>,
  "occurrence": <int>,
  "detection": <int>,
  "rpn": <自动计算>,
  "risk_level": "低/中/高/极高",
  "reasons": "简短调整理由（不超过20字）",
  "suggestion": "简短改进建议（不超过30字）"
}}
只输出一个 JSON 对象，不要包含任何其他内容。
"""
            single_task = Task(
                description=single_prompt,
                expected_output="符合描述格式的 JSON 对象",
                agent=agent,
            )
            single_crew = Crew(
                agents=[agent],
                tasks=[single_task],
                process=Process.sequential,
                verbose=True,
            )
            try:
                single_raw = _kickoff_with_retry(single_crew, label=f"单条评估#{idx}")
                single_text = _safe_raw_text(single_raw)
                _raise_for_empty_response(single_text)
                single_dict = extract_json_robust(single_text)
                if isinstance(single_dict, list):
                    single_dict = single_dict[0] if single_dict else {}
                if isinstance(single_dict, dict):
                    item = _process_defect_item(single_dict, idx)
                    if item:
                        item.ap = calculate_ap(item.severity, item.occurrence, item.detection)
                        if item.rpn >= settings.HIGH_RISK_THRESHOLD:
                            item.review_required = True
                        elif settings.FORCE_SUSPEND_S9 and item.severity >= 9:
                            item.review_required = True
                        else:
                            item.review_required = False
                        result_items.append(item)
            except FMEABaseException as e:
                logger.error(f"单条缺陷评估失败: {e}")
            except Exception as e:
                logger.error(f"单条缺陷评估失败（非业务异常）: {e}")

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
# 端到端快捷接口（提取 + 规则引擎评级 + 审计）
# ══════════════════════════════════════════════════
def run_full_fmea(report_text: str) -> Dict[str, Any]:
    """
    完整的 FMEA 流程：从检验报告文本直接输出最终评估。

    流程：
    1. LLM 提取缺陷（extract_defects）
    2. 规则引擎评级（evaluate_one_defect）
    3. 规则引擎审计（audit_one_defect）
    4. 返回与异步任务一致的结果结构

    Returns:
        dict: {
            "defects": [...],
            "report_summary": str,
            "failed_count": int,
            "failed_defect_ids": []
        }
    """
    logger.info("🚀 开始全流程 FMEA（同步规则引擎路径）：缺陷提取 → 规则评级 → 规则审计")

    # 1. 提取缺陷（LLM）
    extraction_result = extract_defects(report_text)
    # 转换为字典列表
    defects = [defect.model_dump() for defect in extraction_result.defects]

    if not defects:
        return {
            "defects": [],
            "report_summary": "未发现需评估的缺陷",
            "failed_count": 0,
            "failed_defect_ids": [],
        }

    # 2. 逐个评级 + 审计
    results = []
    failed_ids = []
    for defect in defects:
        try:
            # 规则引擎评级
            evaluated = evaluate_one_defect(defect)
            # 规则引擎审计
            audited = audit_one_defect(evaluated)
            results.append(audited)
        except Exception as e:
            logger.error(f"缺陷 {defect.get('id', 'unknown')} 处理失败: {e}", exc_info=True)
            # 失败时构造错误标记，保证结果完整
            error_defect = {
                **defect,
                "error": f"处理失败: {e}",
                "risk_level": "评估失败",
                "rpn": None,
            }
            results.append(error_defect)
            failed_ids.append(defect.get("id"))

    # 3. 组装返回（与异步 full_evaluation_v2 的最终结构保持一致）
    summary = f"共评估 {len(results)} 条缺陷"
    if failed_ids:
        summary += f"，其中 {len(failed_ids)} 条处理失败"

    return {
        "defects": results,
        "report_summary": summary,
        "failed_count": len(failed_ids),
        "failed_defect_ids": failed_ids,
    }


# ══════════════════════════════════════════════════
# 向后兼容：保留旧版函数名及 API
# ══════════════════════════════════════════════════
create_analysis_crew = extract_defects  # 旧版名称映射


def run_fmea_evaluation(text: str) -> dict:
    """
    [兼容旧版] 旧版 FMEA 评估函数，返回纯字典。
    现在调用新的 run_full_fmea 并返回相同结构。
    """
    logger.warning("调用了已废弃的 run_fmea_evaluation，建议迁移到 run_full_fmea")
    return run_full_fmea(text)


def run_full_fmea_evaluation(text: str) -> dict:
    """
    [兼容旧版] 旧版完整评估函数，返回纯字典。
    现在调用新的 run_full_fmea 并返回相同结构。
    """
    logger.warning("调用了已废弃的 run_full_fmea_evaluation，建议迁移到 run_full_fmea")
    return run_full_fmea(text)