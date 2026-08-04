import json
import re
import logging
from crewai import Agent, Task, Crew, Process, Tool
from app.agents import get_llm                      # ⚠️ 不再直接 import llm
from app.core.tools import risk_tool, diagnosis_tool
from app.core.vector_store import search_standards  # ✅ 直接使用预加载的向量库

logger = logging.getLogger("defect_fmea.crews")

# ----- 辅助：从 LLM 输出中提取 JSON -----
def sanitize_llm_output(text: str) -> str:
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end+1]
    return text

def extract_json(obj: str) -> dict:
    cleaned = sanitize_llm_output(obj)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    match = re.search(r"\{.*\}", obj, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    logger.warning("无法解析 LLM 输出为 JSON，返回原始文本")
    return {"raw_output": obj, "parse_error": "无法解析为JSON"}

# ----- 新建：法规检索工具（直接使用 search_standards） -----
def regulation_search_function(query: str) -> str:
    """
    调用预加载的向量库 search_standards，返回格式化的法规信息。
    该函数将作为工具提供给 Agent。
    """
    try:
        results = search_standards(query, k=3)
        if not results:
            return "未找到相关法规条文。"
        # 格式化输出，方便 LLM 提取
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

# 注册为 CrewAI 工具（与现有 risk_tool / diagnosis_tool 风格一致）
search_regulation_tool = Tool(
    name="search_regulation_tool",
    description=(
        "根据查询字符串检索特种设备相关法规条文。"
        "输入参数 query: 构造的查询字符串，例如 '裂纹 等级高 处理措施'。"
        "返回多条相关法规内容及其来源文件。"
    ),
    func=regulation_search_function,
)

# ----- 三条流水线 -----
def create_analysis_crew(text: str) -> str:
    agent = Agent(
        role="特种设备缺陷解析专家",
        goal="提取缺陷输出纯JSON，不包含任何Markdown",
        backstory="只输出事实，不添加解释。",
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )
    task = Task(
        description=(
            "根据报告文本提取所有缺陷，严格按照以下JSON格式输出，不要任何额外文字：\n"
            '{"defects":[{"id":1,"type":"裂纹","component":"容器本体","location":"筒体焊缝",'
            '"dimensions":{"length":30,"depth":2.5,"unit":"mm"},"quantity":1,'
            '"wall_thickness":20,"original_text":"原文"}]}\n'
            "要求：单位统一为mm，每个缺陷必须有original_text字段。\n"
            "⚠️ 如果报告中没有明确的设计壁厚（mm），请将 wall_thickness 设为 null，严禁猜测数值。\n"
            f"报告文本：{text}"
        ),
        expected_output="纯JSON字符串",
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=True)
    result = crew.kickoff()
    return result.raw if hasattr(result, "raw") else str(result)

def run_fmea_evaluation(text: str) -> dict:
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

    parse_task = Task(
        description=(
            "提取所有缺陷，输出纯JSON：\n"
            '{"defects":[{"id":1,"type":"...","component":"...","location":"...",'
            '"dimensions":{"length":...,"depth":...,"unit":"mm"},'
            '"quantity":1,"wall_thickness":20,"original_text":"..."}]}\n'
            "⚠️ 如果报告中没有壁厚（mm），请将 wall_thickness 设为 null，不要虚构数字。\n"
            f"报告文本：{text}"
        ),
        expected_output="仅defects数组的JSON",
        agent=extractor,
    )

    evaluate_task = Task(
        description=(
            "对每条缺陷分别执行：\n"
            "1. 调用 risk_assessment_tool(defect_type=类型, length_mm=长度, depth_mm=深度, "
            "wall_thickness=壁厚, quantity=数量) 得到评级。\n"
            "   【禁止使用任何我未列出的参数名】\n"
            "2. 调用 diagnosis_tool(defect_type=类型) 得到原因列表。\n"
            "3. 将工具返回的数值原样填入JSON，不得修改。\n"
            "最终输出JSON格式：\n"
            '{"report_summary":"...","defects":[{"id":...,"type":"...","quantity":...,'
            '"original_text":"...","severity":...,"occurrence":...,"detection":...,'
            '"rpn":...,"risk_level":"...","level":...,"reasons":[...],'
            '"suggestion":"基于风险的推荐","standard_ref":"...",'
            '"triggered_rules":[...]}]}\n'
            "只输出纯JSON文本，不要Markdown代码块。"
        ),
        expected_output="包含FMEA评级的JSON字符串",
        agent=analyst,
        context=[parse_task],
    )

    crew = Crew(
        agents=[extractor, analyst],
        tasks=[parse_task, evaluate_task],
        process=Process.sequential,
        verbose=True
    )
    result = crew.kickoff()
    raw = result.raw if hasattr(result, "raw") else str(result)
    return extract_json(raw)

def run_full_fmea_evaluation(text: str) -> dict:
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
        tools=[search_regulation_tool],      # ✅ 替换为使用 search_standards 的新工具
        llm=get_llm(),
        verbose=True,
        allow_delegation=False,
    )

    parse_task = Task(
        description=(
            "提取所有缺陷，输出纯JSON：\n"
            '{"defects":[{"id":1,"type":"...","component":"...","location":"...",'
            '"dimensions":{"length":...,"depth":...,"unit":"mm"},'
            '"quantity":1,"wall_thickness":20,"original_text":"..."}]}\n'
            "⚠️ 如果报告中没有壁厚（mm），请将 wall_thickness 设为 null，严禁虚构。\n"
            f"报告文本：{text}"
        ),
        expected_output="仅defects的JSON",
        agent=extractor,
    )

    evaluate_task = Task(
        description=(
            "对每条缺陷分别执行：\n"
            "1. 调用 risk_assessment_tool(defect_type=类型, length_mm=长度, depth_mm=深度, "
            "wall_thickness=壁厚, quantity=数量)。\n"
            "   【禁止使用任何我未列出的参数名】\n"
            "2. 调用 diagnosis_tool(defect_type=类型)。\n"
            "3. 将工具返回值原样填入JSON，严禁修改数字。\n"
            "最终输出JSON格式：\n"
            '{"report_summary":"...","defects":[{"id":...,"type":"...","quantity":...,'
            '"original_text":"...","severity":...,"occurrence":...,"detection":...,'
            '"rpn":...,"risk_level":"...","level":...,"reasons":[...],'
            '"suggestion":"基于风险的推荐","standard_ref":"...","triggered_rules":[...]}]}\n'
            "只输出纯JSON，不要任何Markdown标记。"
        ),
        expected_output="完整FMEA JSON",
        agent=analyst,
        context=[parse_task],
    )

    audit_task = Task(
        description=(
            "对上一步输出的每条缺陷，执行：\n"
            "1. 读取 type 和 level 值。\n"
            "2. 构造查询字符串，例如 f'{type} 等级{level} 处理措施'。\n"
            "3. 调用 search_regulation_tool(query=构造的字符串)。\n"
            "   【只传 query 一个参数】\n"
            "4. 将返回值中的 law_references, mandatory_measures, inspection_advice "
            "原样追加到每条缺陷对象中。\n"
            "最终输出完整增强报告JSON，不得有任何额外说明或Markdown。"
        ),
        expected_output="附加法规信息的JSON",
        agent=auditor,
        context=[evaluate_task],
    )

    crew = Crew(
        agents=[extractor, analyst, auditor],
        tasks=[parse_task, evaluate_task, audit_task],
        process=Process.sequential,
        verbose=True
    )
    result = crew.kickoff()
    raw = result.raw if hasattr(result, "raw") else str(result)
    return extract_json(raw)