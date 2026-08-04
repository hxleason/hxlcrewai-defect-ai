# models.py
from pydantic import BaseModel, Field
from typing import List, Optional

# ---------- 请求模型 ----------
class EvaluationRequest(BaseModel):
    project_id: str = Field(..., description="项目ID")
    device_tag: str = Field(..., description="设备位号或名称，如'2号反应釜'")
    inspection_text: str = Field(..., description="巡检记录的自然语言文本")

# ---------- 响应模型 ----------
class EventInfo(BaseModel):
    device: str = Field(..., description="设备名称")
    part: str = Field(..., description="部位/部件")
    phenomenon: str = Field(..., description="失效现象")
    quantity: int = Field(1, description="缺陷数量")
    length_mm: Optional[float] = Field(None, description="长度/直径(mm)")
    depth_mm: Optional[float] = Field(None, description="深度(mm)")

class Diagnosis(BaseModel):
    causes: List[str] = Field(..., description="可能原因列表")
    rule_refs: Optional[List[str]] = Field(None, description="引用的标准/规则编号")

class RiskAssessment(BaseModel):
    S: int = Field(..., description="严重度")
    O: int = Field(..., description="发生度")
    D: int = Field(..., description="探测度")
    RPN: int = Field(..., description="风险优先级数")
    explanations: List[str] = Field(..., description="评级依据和解释")

class EvaluationResponse(BaseModel):
    event: EventInfo
    diagnosis: Diagnosis
    risk_assessment: RiskAssessment
    recommendations: List[str] = Field(..., description="建议措施列表")