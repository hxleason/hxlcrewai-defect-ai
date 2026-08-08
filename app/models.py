"""
app/models.py
-
数据库 ORM 模型 + CrewAI 结构化输出 Pydantic 模型

包含:
- Project / Task : SQLAlchemy ORM 映射
- DefectExtractionResult / FMEAEvaluationResult : 可选的结构化输出 schema（供未来升级使用）
"""

from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from pydantic import BaseModel, Field
from typing import List, Optional

# ✅ 统一从 database 导入 Base
from app.db.database import Base


# ================================================================
# 一、SQLAlchemy ORM 模型（数据库表）
# ================================================================

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tasks = relationship("Task", back_populates="project")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    type = Column(String(50), nullable=False, comment="任务类型：analysis / evaluation / full")
    status = Column(String(20), default="pending", comment="pending / started / success / failure")
    input_text = Column(Text, nullable=False, comment="原始报告文本")
    progress = Column(Integer, default=0, comment="进度 0-100")
    result = Column(JSON, nullable=True, comment="最终结果 JSON")
    error_message = Column(Text, nullable=True)
    celery_task_id = Column(String(255), unique=True, comment="Celery 任务 ID")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    project = relationship("Project", back_populates="tasks")


# ================================================================
# 二、Pydantic 结构化输出模型（可选，便于后续升级或 API 校验）
# ================================================================

class DefectDimensions(BaseModel):
    length: Optional[float] = Field(None, description="长度(mm)")
    depth: Optional[float] = Field(None, description="深度(mm)")
    unit: str = Field("mm", description="单位")

class DefectItem(BaseModel):
    id: int = Field(..., description="序号")
    type: str = Field(..., description="缺陷类型")
    component: Optional[str] = Field(None, description="部件")
    location: Optional[str] = Field(None, description="位置")
    dimensions: Optional[DefectDimensions] = Field(None, description="尺寸")
    quantity: int = Field(1, description="数量")
    wall_thickness: Optional[float] = Field(None, description="设计壁厚(mm)")
    original_text: str = Field(..., description="原始文本")

class DefectExtractionResult(BaseModel):
    defects: List[DefectItem] = Field(..., description="提取的缺陷列表")

class EvaluatedDefect(BaseModel):
    id: int
    type: str
    quantity: int
    original_text: str
    severity: int
    occurrence: int
    detection: int
    rpn: int
    risk_level: str
    level: int
    reasons: List[str]
    suggestion: str
    standard_ref: Optional[str] = None
    triggered_rules: List[str] = []
    # 法规审核附加字段（Full 模式填充）
    law_references: Optional[List[str]] = None
    mandatory_measures: Optional[List[str]] = None
    inspection_advice: Optional[List[str]] = None

class FMEAEvaluationResult(BaseModel):
    report_summary: str = Field(..., description="报告摘要")
    defects: List[EvaluatedDefect] = Field(..., description="评估后的缺陷列表")