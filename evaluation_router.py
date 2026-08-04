# evaluation_router.py
import os
import json
import re
import traceback
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM

from tools import diagnosis_tool, risk_assessment_tool
from crew_runner import run_evaluation   # 导入全流程函数

router = APIRouter(prefix="/api/v1")

# ================== LLM 配置 ==================
eval_llm = LLM(
    model="deepseek-v4-pro",
    base_url="https://api.deepseek.com/v1",
    api_key=os.getenv("DEEPSEEK_API_KEY", ""),
    temperature=0.1,
    max_tokens=2000,
)

# ================== 请求/响应模型 ==================
class EvaluationRequest(BaseModel):
    project_id: str
    device_tag: str
    inspection_text: str

class EventInfo(BaseModel):
    device: str
    part: str
    phenomenon: str
    quantity: int = 1
    length_mm: Optional[float] = None
    depth_mm: Optional[float] = None

class DiagnosisInfo(BaseModel):
    causes: list[str]
    rule_refs: Optional[list[str]] = None

class RiskAssessmentInfo(BaseModel):
    S: int
    O: int
    D: int
    RPN: int
    explanations: Optional[list[str]] = None

class EvaluationResponse(BaseModel):
    event: EventInfo
    diagnosis: DiagnosisInfo
    risk_assessment: RiskAssessmentInfo
    recommendations: list[str]

class FullEvaluationRequest(BaseModel):
    project_id: str
    device_tag: str
    inspection_text: str

class FullEvaluationResponse(BaseModel):
    status: str
    project_id: str
    device_tag: str
    full_report: dict   # 完整的 FMEA 报告（字典）

# ================== 辅助函数 ==================
def extract_json(text: str) -> dict:
    """从 Crew 输出中提取 JSON 对象"""
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        return json.loads(text[start:end+1])
    raise ValueError("无法从输出中提取 JSON")

# ================== 原有 /evaluate 端点（保持不变但增加健壮性） ==================
@router.post("/evaluate", response_model=EvaluationResponse)
def evaluate_inspection(req: EvaluationRequest):
    try:
        evaluator = Agent(
            role="压力容器失效分析专家",
            goal="根据巡检记录提取缺陷信息、诊断原因、评估风险并输出标准化报告。",
            backstory="你是一名精通GB/T 150、GB/T 26610等特设标准的资深工程师。",
            tools=[diagnosis_tool, risk_assessment_tool],
            llm=eval_llm,
            verbose=True,
            allow_delegation=False,
        )
        evaluation_task = Task(
            description=(
                "用户提供了一段巡检记录文本：\n"
                "{inspection_text}\n\n"
                "请严格按以下步骤处理：\n"
                "1. 提取关键信息：设备名称(device)、部件(part)、失效现象(phenomenon)、"
                "数量(quantity)、缺陷长度(length_mm)、深度(depth_mm)。\n"
                "2. 调用 diagnosis_tool 和 risk_assessment_tool 获取诊断和风险评估结果。\n"
                "3. 生成最终 JSON（键名必须如下）：\n"
                "{{\n"
                '  "event": {{ "device": "...", "part": "...", "phenomenon": "...", '
                '"quantity": ..., "length_mm": ..., "depth_mm": ... }},\n'
                '  "diagnosis": {{ "causes": [...], "rule_refs": [...] }},\n'
                '  "risk_assessment": {{ "S": ..., "O": ..., "D": ..., "RPN": ..., '
                '"explanations": [...] }},\n'
                '  "recommendations": [...]\n'
                "}}\n"
                "最终只输出 JSON 本身，不要添加任何额外文字。"
            ),
            expected_output="一个严格符合上述结构的 JSON 字符串。",
            agent=evaluator,
        )
        crew = Crew(agents=[evaluator], tasks=[evaluation_task], process=Process.sequential, verbose=True)
        result = crew.kickoff(inputs={"inspection_text": req.inspection_text})
        raw = result.raw if hasattr(result, 'raw') else str(result)
        data = extract_json(raw)
        return EvaluationResponse(**data)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"FMEA分析失败: {str(e)}")

# ================== 新增 /full-evaluation 端点（直接返回完整报告字典） ==================
@router.post("/full-evaluation", response_model=FullEvaluationResponse)
def full_evaluation_inspection(req: FullEvaluationRequest):
    """三阶段全流程评估，返回标准化 FMEA 报告"""
    try:
        # 调用 crew_runner 中的函数，它已经返回解析好的字典
        report_dict = run_evaluation(req.inspection_text)

        return FullEvaluationResponse(
            status="success",
            project_id=req.project_id,
            device_tag=req.device_tag,
            full_report=report_dict
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"全流程评估失败: {str(e)}")