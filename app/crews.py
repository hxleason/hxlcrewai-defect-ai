"""
app/crews.py
特种设备缺陷评估 Crew 流水线（统一配置版 v2.0）
兼容 crewai >= 0.30，使用 @tool 装饰器

特性：
- 所有 LLM 配置统一由 app.core.config.settings 提供，杜绝分散读取
- 使用 LLM 单例避免重复初始化（线程安全）
- 所有任务描述使用三引号模板，杜绝字符串拼接语法错误
- extract_json_robust 多级修复确保 JSON 解析永不崩溃
- 完整 FMEA 评估 + 法规审核流水线
- 统一的自定义异常体系，精确定位错误类型
"""

import re
import logging
from typing import Optional, Dict, Any
from functools import lru_cache

from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import tool
from json_repair import repair_json

# ── 统一配置中心 ──
from app.core.config import settings

# 内部工具模块
from app.core.tools import risk_tool, diagnosis_tool
from app.core.vector_store import search_standards

# 自定义异常体系
from app.core.exceptions import (
    FMEABaseException,
    LLMTimeoutError,
    LLMAPIError,
    ParsingError,
)

logger = logging.getLogger("defect_fmea.crews")

# ================================================================
# LLM 单例（线程安全，使用 lru_cache 保证只初始化一次）
# ================================================================

@lru_cache(maxsize=1)
def get_llm() -> LLM:
    """创建或获取共享的 LLM 实例，配置由 app.core.config.settings 统一提供

    Raises:
        LLMAPIError: 当 API Key 未设置时抛出
    """
    model = settings.LLM_MODEL
    base_url = settings.LLM_BASE_URL
    api_key = settings.LLM_API_KEY
    temperature = settings.LLM_TEMPERATURE
    max_tokens = settings.LLM_MAX_TOKENS

    if not api_key:
        raise LLMAPIError(
            "LLM_API_KEY 未设置，请在 .env 文件中配置有效的 API Key "
            "（推荐使用标准变量名 LLM_API_KEY）"
        )

    logger.info(
        "正在初始化 LLM | model=%s | base_url=%s | temperature=%s | max_tokens=%s",
        model, base_url, temperature, max_tokens,
    )
    instance = LLM(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    logger.info("✅ LLM 实例化成功")
    return instance


# ================================================================
# 通用工具：稳健 JSON 提取（混合策略核心）
# ================================================================

def extract_json_robust(raw_text: str) -> Dict[str, Any]:
    """
    多级 JSON 提取/修复，确保返回可用字典。
    策略：
      1. 输入已是 dict，直接返回
      2. 直接 json.loads
      3. 清理 Markdown 标记后 json.loads
      4. json_repair 智能修复
      5. 正则捕获最大 JSON 块 + json_repair
    终末兜底返回包含原始文本的 dict (含 parse_error 键)
    """
    import json  # 局部导入避免覆盖系统 json

    # 0. 已是字典
    if isinstance(raw_text, dict):
        return raw_text

    # 1. 直接解析
    try:
        return json.loads(raw_text)
    except Exception:
        pass

    # 2. 常规清理（去除 ```json...```）
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
            return {"data": repaired}
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

    # 5. 彻底失败，返回原始文本供调试
    logger.warning("⚠️ 所有 JSON 解析/修复步骤均失败，返回原始文本")
    return {"raw_output": raw_text, "parse_error": "无法解析为JSON"}


# ================================================================
# 法规检索工具（@tool 装饰器）
# ================================================================

@tool("search_regulation_tool")
def regulation_search_function(query: str) -> str:
    """根据查询字符串检索特种设备相关法规条文。

    输入参数 query: 构造的查询字符串，例如 '裂纹 等级高 处理措施'
    返回多条相关法规内容及其来源文件。
    """
    try:
        results = search_standards(query, k=3)
        if not results:
            return "未找到相关法规条文。"
        formatted = []
        for i, item in enumerate(results, 1):
            formatted.append(
                f"法规 {i}（来源：{item.get('source', '未知文件')}）:\n"
                f"{item.get('content', '')}\n"
            )
        return "\n".join(formatted)
    except Exception as e:
        logger.error(f"search_standards 调用失败: {e}")
        return f"法规检索失败：{str(e)}"


search_regulation_tool = regulation_search_function  # 别名方便引用


# ================================================================
# 内部异常转换辅助函数
# ================================================================

def _classify_llm_exception(e: Exception) -> FMEABaseException:
    """根据异常内容判断是否为 LLM 超时或 API 错误，并返回对应的自定义异常"""
    msg = str(e).lower()
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return LLMTimeoutError(f"LLM 调用超时: {e}")
    if "api key" in msg or "authentication" in msg or "unauthorized" in msg:
        return LLMAPIError(f"LLM 认证失败: {e}")
    if "rate limit" in msg or "quota" in msg:
        return LLMAPIError(f"LLM 额度或频率限制: {e}")
    return FMEABaseException(f"评估流程异常: {e}", error_code="EVALUATION_FAILED", status_code=500)


# ================================================================
# 流水线 1：缺陷提取（返回原始字符串，供简单调用）
# ================================================================

def create_analysis_crew(report_text: str) -> str:
    """快速提取缺陷 JSON，返回 LLM 的原始输出字符串
    
    Raises:
        FMEABaseException: 子类异常，根据具体错误类型抛出
    """
    try:
        agent = Agent(
            role="特种设备缺陷解析专家",
            goal="提取缺陷输出纯JSON，不包含任何Markdown",
            backstory="只输出事实，不添加解释。",
            llm=get_llm(),
            verbose=True,
            allow_delegation=False,
        )
        task_desc = f"""根据报告文本提取所有缺陷，严格输出以下 JSON 格式，不要任何额外文字：
{{
  "defects": [
    {{
      "id": 1,
      "type": "裂纹",
      "component": "容器本体",
      "location": "筒体焊缝",
      "dimensions": {{ "length": 30, "depth": 2.5, "unit": "mm" }},
      "quantity": 1,
      "wall_thickness": 20,
      "original_text": "原文"
    }}
  ]
}}
⚠️ 如果报告中没有明确的设计壁厚（mm），请将 wall_thickness 设为 null，严禁猜测数值。
报告文本：{report_text}"""

        task = Task(
            description=task_desc,
            expected_output="纯JSON字符串",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=True)
        result = crew.kickoff()
        raw = result.raw if hasattr(result, "raw") else str(result)
        return raw

    except FMEABaseException:
        raise
    except Exception as e:
        logger.error(f"缺陷提取任务失败: {e}")
        raise _classify_llm_exception(e) from e


# ================================================================
# 流水线 2：缺陷评估（FMEA 评级）→ 返回 dict
# ================================================================

def run_fmea_evaluation(report_text: str) -> dict:
    """执行 FMEA 评估（提取 + 风险评级 + 原因分析），返回可靠字典

    Raises:
        ParsingError: 当 LLM 输出无法解析为 JSON 时
        LLMTimeoutError: 当 LLM 请求超时时
        LLMAPIError: 当 LLM 接口调用失败时
        FMEABaseException: 其他评估流程异常
    """
    extractor = Agent(
        role="特种设备缺陷解析专家",
        goal="提取缺陷为JSON",
        backstory="纯数据提取，禁止编造数值。",
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )
    analyst = Agent(
        role="FMEA评级分析师",
        goal="调用工具获取评级，严禁自创",
        backstory="必须用 risk_assessment_tool 和 diagnosis_tool，只使用我列出的参数名。",
        tools=[risk_tool, diagnosis_tool],
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )

    parse_desc = f"""提取所有缺陷，输出纯 JSON：
{{
  "defects": [
    {{
      "id": 1,
      "type": "...",
      "component": "...",
      "location": "...",
      "dimensions": {{ "length": null, "depth": null, "unit": "mm" }},
      "quantity": 1,
      "wall_thickness": null,
      "original_text": "..."
    }}
  ]
}}
⚠️ 如果报告中没有壁厚（mm），请将 wall_thickness 设为 null，不要虚构数字。
报告文本：{report_text}"""

    parse_task = Task(
        description=parse_desc,
        expected_output="仅 defects 数组的 JSON",
        agent=extractor,
    )

    eval_desc = """对每条缺陷分别执行：
1. 调用 risk_assessment_tool(defect_type=类型, length_mm=长度, depth_mm=深度, wall_thickness=壁厚, quantity=数量)
   【禁止使用任何我未列出的参数名】
2. 调用 diagnosis_tool(defect_type=类型) 得到原因列表。
3. 将工具返回的数值原样填入 JSON，不得修改。
最终输出 JSON 格式：
{
  "report_summary": "...",
  "defects": [
    {
      "id": 1,
      "type": "...",
      "quantity": 1,
      "original_text": "...",
      "severity": 8,
      "occurrence": 4,
      "detection": 6,
      "rpn": 192,
      "risk_level": "高",
      "level": 3,
      "reasons": ["原因1", "原因2"],
      "suggestion": "基于风险的推荐",
      "standard_ref": "GB/T ...",
      "triggered_rules": ["规则A", "规则B"]
    }
  ]
}
只输出纯 JSON 文本，不要 Markdown 代码块。"""

    eval_task = Task(
        description=eval_desc,
        expected_output="包含 FMEA 评级的 JSON 字符串",
        agent=analyst,
        context=[parse_task],
    )

    crew = Crew(
        agents=[extractor, analyst],
        tasks=[parse_task, eval_task],
        process=Process.sequential,
        verbose=True,
    )

    try:
        result = crew.kickoff()
    except Exception as e:
        logger.error(f"FMEA Crew 执行失败: {e}")
        raise _classify_llm_exception(e) from e

    raw = result.raw if hasattr(result, "raw") else str(result)
    logger.info("LLM 原始输出 (FMEA):\n%s", raw[:500])

    parsed = extract_json_robust(raw)
    if "parse_error" in parsed:
        raise ParsingError(
            f"LLM 输出无法解析为 JSON。原始输出片段: {raw[:300]}"
        )
    return parsed


# ================================================================
# 流水线 3：完整评估（FMEA + 法规审核）→ 返回 dict
# ================================================================

def run_full_fmea_evaluation(report_text: str) -> dict:
    """执行完整 FMEA 评估 + 法规审核，返回包含法律依据的增强报告字典

    Raises:
        ParsingError: 当 LLM 输出无法解析为 JSON 时
        LLMTimeoutError: 当 LLM 请求超时时
        LLMAPIError: 当 LLM 接口调用失败时
        FMEABaseException: 其他评估流程异常
    """
    extractor = Agent(
        role="特种设备缺陷解析专家",
        goal="提取缺陷为JSON",
        backstory="只提取数据，禁止编造数值。",
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )
    analyst = Agent(
        role="FMEA评级分析师",
        goal="调用工具获取评级，禁止自创",
        backstory="必须用 risk_assessment_tool 和 diagnosis_tool，仅用我列出的参数名。",
        tools=[risk_tool, diagnosis_tool],
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )
    auditor = Agent(
        role="法规审核专家",
        goal="使用 search_regulation_tool 为每条缺陷补充法规条文",
        backstory="只能调用工具获取法规，参数只用 query。",
        tools=[search_regulation_tool],
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )

    parse_desc = f"""提取所有缺陷，输出纯 JSON：
{{
  "defects": [
    {{
      "id": 1,
      "type": "...",
      "component": "...",
      "location": "...",
      "dimensions": {{ "length": null, "depth": null, "unit": "mm" }},
      "quantity": 1,
      "wall_thickness": null,
      "original_text": "..."
    }}
  ]
}}
⚠️ 如果报告中没有壁厚（mm），请将 wall_thickness 设为 null，严禁虚构。
报告文本：{report_text}"""

    parse_task = Task(
        description=parse_desc,
        expected_output="仅 defects 的 JSON",
        agent=extractor,
    )

    eval_desc = """对每条缺陷分别执行：
1. 调用 risk_assessment_tool(defect_type=类型, length_mm=长度, depth_mm=深度, wall_thickness=壁厚, quantity=数量)
   【禁止使用任何我未列出的参数名】
2. 调用 diagnosis_tool(defect_type=类型)
3. 将工具返回的数值原样填入 JSON，严禁修改。
最终输出 JSON 格式：
{
  "report_summary": "...",
  "defects": [
    {
      "id": 1,
      "type": "...",
      "quantity": 1,
      "original_text": "...",
      "severity": 8,
      "occurrence": 4,
      "detection": 6,
      "rpn": 192,
      "risk_level": "高",
      "level": 3,
      "reasons": ["原因1", "原因2"],
      "suggestion": "基于风险的推荐",
      "standard_ref": "GB/T ...",
      "triggered_rules": ["规则A", "规则B"]
    }
  ]
}
只输出纯 JSON，不要任何 Markdown 标记。"""

    eval_task = Task(
        description=eval_desc,
        expected_output="完整 FMEA JSON",
        agent=analyst,
        context=[parse_task],
    )

    audit_desc = """对上一步输出的每条缺陷，执行：
1. 读取 type 和 level 值。
2. 构造查询字符串，例如 f'{type} 等级{level} 处理措施'。
3. 调用 search_regulation_tool(query=构造的字符串)。
   【只传 query 一个参数】
4. 将返回值中的 law_references, mandatory_measures, inspection_advice
   原样追加到每条缺陷对象中。
最终输出完整增强报告 JSON，不得有任何额外说明或 Markdown。"""

    audit_task = Task(
        description=audit_desc,
        expected_output="附加法规信息的 JSON",
        agent=auditor,
        context=[eval_task],
    )

    crew = Crew(
        agents=[extractor, analyst, auditor],
        tasks=[parse_task, eval_task, audit_task],
        process=Process.sequential,
        verbose=True,
    )

    try:
        result = crew.kickoff()
    except Exception as e:
        logger.error(f"完整评估 Crew 执行失败: {e}")
        raise _classify_llm_exception(e) from e

    raw = result.raw if hasattr(result, "raw") else str(result)
    logger.info("LLM 原始输出 (完整评估):\n%s", raw[:500])

    parsed = extract_json_robust(raw)
    if "parse_error" in parsed:
        raise ParsingError(
            f"完整评估 LLM 输出无法解析为 JSON。原始输出片段: {raw[:300]}"
        )
    return parsed