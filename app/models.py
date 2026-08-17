"""
app/models.py
─────────────────────────────────────────────────────────────
数据库 ORM 模型 + CrewAI 结构化输出 Pydantic 模型

包含:
- Project / Task / Defect : SQLAlchemy ORM 映射
- DefectExtractionResult / FMEAEvaluationResult : 可选结构化输出 schema
─────────────────────────────────────────────────────────────
增强记录：
- Task 新增 started_at、last_heartbeat 字段，用于任务状态监控与僵尸任务检测。
- 新增 Defect 模型，用于持久化存储缺陷及 FMEA 评估结果，便于查询与统计。
"""

from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, ForeignKey, Float
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from pydantic import BaseModel, Field
from typing import List, Optional

# ✅ 统一从 database 导入 Base
from app.db.database import Base


# ══════════════════════════════════════════════════
# 一、SQLAlchemy ORM 模型（数据库表）
# ══════════════════════════════════════════════════

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
    result = Column(JSON, nullable=True, comment="最终结果 JSON（成功时存储结构化数据，失败时包含 error_code 等）")
    error_message = Column(Text, nullable=True)
    celery_task_id = Column(String(255), unique=True, comment="Celery 任务 ID")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True, comment="任务开始执行时间")
    last_heartbeat = Column(DateTime(timezone=True), nullable=True, comment="任务最后心跳时间")
    completed_at = Column(DateTime(timezone=True), nullable=True)

    project = relationship("Project", back_populates="tasks")
    defects = relationship("Defect", back_populates="task", cascade="all, delete-orphan")


class Defect(Base):
    """
    缺陷记录表 - 持久化存储从原始报告中提取的缺陷及其 FMEA 评估结果。
    一个任务（分析或评估）可以生成多个缺陷记录。
    评估字段（severity 等）允许为空，表示尚未执行评估。
    """
    __tablename__ = "defects"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True, comment="所属任务 ID")

    # --- 缺陷提取信息 ---
    defect_type = Column(String(100), nullable=False, comment="缺陷类型")
    component = Column(String(200), nullable=True, comment="部件")
    location = Column(String(200), nullable=True, comment="位置")
    length = Column(Float, nullable=True, comment="长度(mm)")
    depth = Column(Float, nullable=True, comment="深度(mm)")
    unit = Column(String(10), default="mm", comment="单位")
    quantity = Column(Integer, default=1, comment="数量")
    wall_thickness = Column(Float, nullable=True, comment="设计壁厚(mm)")
    original_text = Column(Text, nullable=False, comment="原始文本")

    # --- FMEA 评估结果（可为空，表示尚未评估） ---
    severity = Column(Integer, nullable=True, comment="严重度 S")
    occurrence = Column(Integer, nullable=True, comment="发生度 O")
    detection = Column(Integer, nullable=True, comment="检测度 D")
    rpn = Column(Integer, nullable=True, comment="风险优先数 RPN")
    risk_level = Column(String(20), nullable=True, comment="风险等级: 低/中/高/严重")
    level = Column(Integer, nullable=True, comment="风险等级数字")
    reasons = Column(JSON, nullable=True, comment="触发原因列表(字符串数组)")
    suggestion = Column(Text, nullable=True, comment="改进建议")
    standard_ref = Column(String(500), nullable=True, comment="引用标准编号")
    triggered_rules = Column(JSON, nullable=True, comment="触发的规则列表")
    # 法规审核附加字段（Full 模式填充）
    law_references = Column(JSON, nullable=True, comment="法规引用列表")
    mandatory_measures = Column(JSON, nullable=True, comment="强制措施列表")
    inspection_advice = Column(JSON, nullable=True, comment="检验建议列表")
    # 扩展字段
    extra_data = Column(JSON, nullable=True, comment="其他扩展数据（可放自定义字段）")

    # --- 时间戳 ---
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    # --- 关系 ---
    task = relationship("Task", back_populates="defects")


# ══════════════════════════════════════════════════
# 二、Pydantic 结构化输出模型（可选，便于后续扩展或 API 校验）
# ══════════════════════════════════════════════════

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