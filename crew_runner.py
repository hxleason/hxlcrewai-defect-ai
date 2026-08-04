# crew_runner.py
import json
import re
import logging
from crewai import Agent, Task, Crew, Process, LLM
from tools import diagnosis_tool, risk_assessment_tool, search_regulation_tool

logger = logging.getLogger(__name__)

# ================== LLM 配置 ==================
eval_llm = LLM(
    model="deepseek-v4-pro",
    base_url="https://api.deepseek.com/v1",
    api_key="sk-f7f60a19b9154a6b9781f40c4759b6f4",
    temperature=0.1,
    max_tokens=4096,          # ★ 增加输出长度，防止截断
)

# ================== 全流程评估函数 ==================
def run_evaluation(inspection_text: str) -> dict:
    """
    执行三阶段 FMEA 评估：
    1) 缺陷提取  2) 诊断+评级  3) 法规审核
    返回完整的报告字典（已解析 JSON）。
    """
    # ---------- Agent 定义 ----------
    extractor = Agent(
        role="压力容器缺陷提取专家",
        goal="从巡检文本中提取所有缺陷信息，结构化输出。",
        backstory="你擅长阅读特设检验报告，提取关键缺陷数据。",
        llm=eval_llm,
        verbose=True,
        allow_delegation=False,
    )

    analyst = Agent(
        role="FMEA 评级分析师",
        goal="对提取的缺陷进行诊断和风险评级，输出 FMEA 报告。",
        backstory="你精通 FMEA 方法论和压力容器失效模式。",
        tools=[diagnosis_tool, risk_assessment_tool],
        llm=eval_llm,
        verbose=True,
        allow_delegation=False,
    )

    reviewer = Agent(
        role="特种设备法规审核专家",
        goal="为 FMEA 报告补充法规依据、强制措施和检验建议。",
        backstory="你熟知 TSG 21、GB/T 150、GB/T 19624 等法规标准。",
        tools=[search_regulation_tool],
        llm=eval_llm,
        verbose=True,
        allow_delegation=False,
    )

    # ---------- Task 定义 ----------
    task1 = Task(
        description=(
            "用户提供的巡检记录如下：\n"
            "{inspection_text}\n\n"
            "请提取所有缺陷，输出 JSON 数组，每个缺陷包含：\n"
            "id, type(裂纹/点蚀等), component, location, quantity, original_text, "
            "length_mm, depth_mm, wall_thickness_mm。\n"
            "只输出 JSON 数组，不要其他内容。"
        ),
        expected_output="一个 JSON 数组字符串。",
        agent=extractor,
    )

    task2 = Task(
        description=(
            "你收到了上一步提取的缺陷数据。请对每一条缺陷：\n"
            "1. 调用 diagnosis_tool 获取失效原因列表\n"
            "2. 调用 risk_assessment_tool 获取 S/O/D/RPN 和风险等级\n"
            "3. 生成简明的处理建议（每条不超过60个字）\n"
            "整合成如下 JSON 格式：\n"
            "{{\n"
            '  "report_summary": "整体风险概述",\n'
            '  "defects": [\n'
            '    {{\n'
            '      "id": 1, "type": "...", "component": "...", "quantity": 1,\n'
            '      "original_text": "...",\n'
            '      "severity": 7, "occurrence": 4, "detection": 5, "rpn": 140,\n'
            '      "risk_level": "中", "level": 2,\n'
            '      "reasons": ["原因1"],\n'
            '      "suggestion": "简短处理措施"\n'
            '    }}\n'
            '  ]\n'
            "}}\n"
            "输出必须为纯 JSON，不得有额外说明。"
        ),
        expected_output="一个严格符合结构的 JSON 字符串。",
        agent=analyst,
    )

    task3 = Task(
        description=(
            "你收到了上一步的 FMEA 报告。请为每条缺陷补充法规信息：\n"
            "1. 调用 search_regulation_tool 查询相关法规条款\n"
            "2. 提取 law_references (法规条款号)\n"
            "3. 提取 mandatory_measures (强制措施，每条不超过80字)\n"
            "4. 提取 inspection_advice (检验建议，每条不超过80字)\n"
            "5. 将所有字段添加到每条缺陷对象中，保持原结构不变\n"
            "如果工具返回错误，则基于自身知识生成（需注明'基于经验'）。\n"
            "最终输出增强后的完整 JSON，不要任何额外文字。"
        ),
        expected_output="一个完整的增强后 JSON 字符串。",
        agent=reviewer,
    )

    # ---------- 创建 Crew ----------
    crew = Crew(
        agents=[extractor, analyst, reviewer],
        tasks=[task1, task2, task3],
        process=Process.sequential,
        verbose=True,
    )

    # ---------- 运行并获得原始输出 ----------
    result = crew.kickoff(inputs={"inspection_text": inspection_text})
    raw_output = result.raw if hasattr(result, 'raw') else str(result)
    logger.info("Crew 原始输出:\n%s", raw_output)

    # ---------- JSON 提取与修复 ----------
    try:
        # 尝试提取 JSON 块
        match = re.search(r'```(?:json)?\s*(.*?)\s*```', raw_output, re.DOTALL)
        if match:
            json_str = match.group(1)
        else:
            # 直接找花括号
            start = raw_output.find('{')
            end = raw_output.rfind('}')
            if start != -1 and end != -1:
                json_str = raw_output[start:end+1]
            else:
                raise ValueError("未找到 JSON 对象")

        # 尝试解析，如果失败则尝试修复截断
        try:
            report = json.loads(json_str)
        except json.JSONDecodeError:
            # 可能被截断，尝试修复：补齐缺少的括号/引号
            logger.warning("JSON 不完整，尝试自动修复...")
            # 简单补全：如果最后一个字符不是 }，则补充
            fixed = json_str.strip()
            # 数括号数量
            missing_braces = fixed.count('{') - fixed.count('}')
            fixed += '}' * missing_braces
            missing_brackets = fixed.count('[') - fixed.count(']')
            fixed += ']' * missing_brackets
            # 移除尾部的逗号
            fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
            # 再次尝试
            try:
                report = json.loads(fixed)
            except json.JSONDecodeError:
                # 如果仍然失败，提取第一个完整的缺陷对象
                logger.error("自动修复失败，回退到部分结果")
                report = {
                    "report_summary": "报告生成中发生截断，以下为部分可用数据",
                    "defects": extract_partial_defects(fixed)
                }
        return report

    except Exception as e:
        # 兜底：返回原始文本
        logger.error("JSON 提取彻底失败: %s", str(e))
        return {
            "report_summary": "FMEA 分析失败",
            "error": str(e),
            "raw_output": raw_output[:500]  # 只保留前500字符
        }

def extract_partial_defects(text: str) -> list:
    """尝试从截断的文本中提取已存在的缺陷对象"""
    defects = []
    # 正则匹配每个完整的缺陷对象
    pattern = r'\{\s*"id":\s*\d+.*?\}'
    matches = re.finditer(pattern, text, re.DOTALL)
    for m in matches:
        block = m.group()
        try:
            obj = json.loads(block)
            defects.append(obj)
        except:
            continue
    return defects