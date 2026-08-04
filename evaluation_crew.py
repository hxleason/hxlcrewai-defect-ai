# evaluation_crew.py
from crewai import Agent, Task, Crew, Process
from langchain_deepseek import ChatDeepSeek   # 你的 LLM
from tools import diagnosis_tool, risk_assessment_tool
import os

# 配置 LLM（已设置环境变量 DEEPSEEK_API_KEY）
llm = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0.1,          # 低温度保证稳定输出
)

# 定义 Agent（注意：tools 只放这两个工具）
evaluator = Agent(
    role="压力容器失效分析专家",
    goal="根据巡检记录的文本，首先提取出缺陷的关键参数（defect_type, quantity, length_mm, depth_mm），然后利用诊断和风险评估工具进行分析，最后生成标准化的 JSON 报告。",
    backstory="你是一名经验丰富的化工设备安全工程师，熟悉GB/T 150、GB/T 26610等标准，擅长从现场描述中捕捉风险信号。",
    tools=[diagnosis_tool, risk_assessment_tool],   # 只有这两个工具
    llm=llm,
    verbose=True,
    allow_delegation=False
)

# 定义任务（让 Agent 自己完成信息提取和工具调用）
evaluation_task = Task(
    description="""用户将提供一段巡检记录文本。
你的工作流程：
1. 仔细阅读巡检文本，从中提取以下参数：
   - defect_type：缺陷类型（如裂纹、腐蚀、减薄、变形等）
   - quantity：缺陷数量，未提及时默认1
   - length_mm：缺陷长度或直径，单位毫米，未提及时默认0
   - depth_mm：缺陷深度，单位毫米，未提及时默认0

2. 使用提取到的参数，**依次调用** diagnosis_tool 和 risk_assessment_tool，获取诊断结果和风险评估数据。
   调用时必须使用准确的参数名：defect_type, quantity, length_mm, depth_mm。

3. 综合所有信息，生成最终的 JSON 分析报告。报告必须严格遵循下面的 JSON 结构（键名完全一致，不要增减字段），可以用 null 表示缺失的数值：
{
  "event": {
    "device": "设备名称",
    "part": "部位/部件",
    "phenomenon": "失效现象",
    "quantity": 1,
    "length_mm": 150.0,
    "depth_mm": 3.2
  },
  "diagnosis": {
    "causes": ["原因1", "原因2"],
    "rule_refs": ["标准条款1"]
  },
  "risk_assessment": {
    "S": 7,
    "O": 4,
    "D": 5,
    "RPN": 140,
    "explanations": ["解释1", "解释2"]
  },
  "recommendations": ["建议1", "建议2"]
}

注意：
- event 中的 device, part, phenomenon 必须从巡检文本中准确提取。
- diagnosis 中的 rule_refs 如果工具没有提供，可以设为 null。
- recommendations 至少提供 2 条具体可行的措施。
- 最终输出必须是纯 JSON，不要加任何额外的解释或 Markdown 标记（除了 JSON 代码块本身）。""",
    expected_output="一个符合上述结构的 JSON 对象。",
    agent=evaluator
)

# 组建 Crew
crew = Crew(
    agents=[evaluator],
    tasks=[evaluation_task],
    process=Process.sequential,
    verbose=True
)