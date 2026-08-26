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
- Defect 新增 pwht_advice 字段（JSON），用于持久化 GB/T 30583-2026 焊后热处理修复工艺建议。
─────────────────────────────────────────────────────────────
注意：
- 下方 Pydantic 模型（DefectItem / EvaluatedDefect 等）属于历史遗留的
  CrewAI 结构化输出 schema，实际 API 响应以 app/schemas.py 为准。
- 保留它们是为了向后兼容旧版 crews.py / 旧测试脚本，请勿在新增代码中引用。
"""

from typing import List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

# ✅ 统一从 database 导入 Base
from app.db.database import Base


# ══════════════════════════════════════════════════
# 一、SQLAlchemy ORM 模型（数据库表）
# ══════════════════════════════════════════════════

class Project(Base):
    """项目表：关联一组分析/评估任务"""

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), index=True, nullable=False, comment="项目名称")
    description = Column(String(500), nullable=True, comment="项目描述")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )

    tasks = relationship(
        "Task", back_populates="project", cascade="all, delete-orphan"
    )


class Task(Base):
    """任务表：记录分析 / 评估 / 全流程任务的执行状态与结果"""

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id"), nullable=True, index=True,
        comment="所属项目 ID（可空，表示独立任务）",
    )
    type = Column(
        String(50),
        nullable=False,
        comment="任务类型：analysis / evaluation / full",
    )
    status = Column(
        String(20),
        default="pending",
        index=True,
        comment="pending / started / success / failure / pending_review / partial_failure",
    )
    input_text = Column(Text, nullable=False, comment="原始报告文本")
    progress = Column(Integer, default=0, comment="进度 0-100")
    result = Column(
        JSON,
        nullable=True,
        comment="最终结果 JSON（成功时存储结构化数据，失败时包含 error_code 等）",
    )
    error_message = Column(Text, nullable=True, comment="错误信息")
    celery_task_id = Column(
        String(255), unique=True, comment="Celery 任务 ID"
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )
    started_at = Column(
        DateTime(timezone=True), nullable=True, comment="任务开始执行时间"
    )
    last_heartbeat = Column(
        DateTime(timezone=True), nullable=True, comment="任务最后心跳时间"
    )
    completed_at = Column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )

    project = relationship("Project", back_populates="tasks")
    defects = relationship(
        "Defect",
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Defect(Base):
    """
    缺陷记录表 - 持久化存储从原始报告中提取的缺陷及其 FMEA 评估结果。

    一个任务（分析或评估）可以生成多个缺陷记录。
    评估字段（severity 等）允许为空，表示尚未执行评估。

    ★ pwht_advice：
      GB/T 30583-2026 焊后热处理（PWHT）修复工艺建议，
      为结构化 JSON，示例：
      {
          "required": true,
          "method": "局部加热",
          "min_temperature_c": 600,
          "max_temperature_c": 640,
          "holding_time_minutes": 60,
          "heating_rate_c_per_hour": 50,
          "cooling_rate_c_per_hour": 50,
          "note": "加热带宽 ≥ 焊缝两侧各 3 倍壁厚"
      }
    """

    __tablename__ = "defects"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属任务 ID",
    )

    # ---------------- 缺陷提取信息 ----------------
    defect_type = Column(String(100), nullable=False, index=True, comment="缺陷类型")
    component = Column(String(200), nullable=True, comment="部件")
    location = Column(String(200), nullable=True, comment="位置")
    length = Column(Float, nullable=True, comment="长度(mm)")
    depth = Column(Float, nullable=True, comment="深度(mm)")
    unit = Column(String(10), default="mm", comment="单位")
    quantity = Column(Integer, default=1, comment="数量")
    wall_thickness = Column(Float, nullable=True, comment="设计壁厚(mm)")
    original_text = Column(Text, nullable=False, comment="原始文本")

    # ---------------- FMEA 评估结果（可为空，表示尚未评估） ----------------
    severity = Column(Integer, nullable=True, comment="严重度 S")
    occurrence = Column(Integer, nullable=True, comment="发生度 O")
    detection = Column(Integer, nullable=True, comment="检测度 D")
    rpn = Column(Integer, nullable=True, comment="风险优先数 RPN")
    risk_level = Column(
        String(20), nullable=True, index=True, comment="风险等级: 低/中/高/极高"
    )
    level = Column(Integer, nullable=True, comment="风险等级数字（1-4）")
    reasons = Column(JSON, nullable=True, comment="触发原因列表(字符串数组)")
    suggestion = Column(Text, nullable=True, comment="改进建议")
    standard_ref = Column(String(500), nullable=True, comment="引用标准编号")
    triggered_rules = Column(JSON, nullable=True, comment="触发的规则列表")

    # ---------------- 法规审核附加字段（Full 模式填充） ----------------
    law_references = Column(JSON, nullable=True, comment="法规引用列表")
    mandatory_measures = Column(JSON, nullable=True, comment="强制措施列表")
    inspection_advice = Column(JSON, nullable=True, comment="检验建议列表")

    # ---------------- 扩展字段 ----------------
    extra_data = Column(
        JSON, nullable=True, comment="其他扩展数据（可放自定义字段）"
    )

    # ★ 新增：PWHT 修复工艺建议（v7.4.1 热修复）
    pwht_advice = Column(
        JSON,
        nullable=True,
        comment="PWHT焊后热处理修复工艺建议(结构化JSON)",
    )

    # ---------------- 时间戳 ----------------
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )
    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now(),
        nullable=True,
        comment="最后更新时间",
    )

    # ---------------- 关系 ----------------
    task = relationship("Task", back_populates="defects")


# ══════════════════════════════════════════════════
# 二、Pydantic 结构化输出模型（历史遗留，供 CrewAI 旧接口使用）
# ══════════════════════════════════════════════════

class DefectDimensions(BaseModel):
    """旧版缺陷尺寸模型"""

    length: Optional[float] = Field(None, description="长度(mm)")
    depth: Optional[float] = Field(None, description="深度(mm)")
    unit: str = Field("mm", description="单位")


class DefectItem(BaseModel):
    """旧版缺陷条目模型（使用 'type' 字段而非 'defect_type'）"""

    id: int = Field(..., description="序号")
    type: str = Field(..., description="缺陷类型")
    component: Optional[str] = Field(None, description="部件")
    location: Optional[str] = Field(None, description="位置")
    dimensions: Optional[DefectDimensions] = Field(None, description="尺寸")
    quantity: int = Field(1, description="数量")
    wall_thickness: Optional[float] = Field(None, description="设计壁厚(mm)")
    original_text: str = Field(..., description="原始文本")


class DefectExtractionResult(BaseModel):
    """旧版缺陷提取结果模型（实际提取流程以 schemas.DefectExtractionResult 为准）"""

    defects: List[DefectItem] = Field(..., description="提取的缺陷列表")


class EvaluatedDefect(BaseModel):
    """旧版评估后缺陷模型（实际 API 响应以 schemas.DefectOut 为准）"""

    id: int
    type: str
    quantity: int
    original_text: str
    severity: int
    occurrence: int
    detection: int
    rpn: int
    risk_level: str  # 低/中/高/极高
    level: int  # 1-4
    reasons: List[str]
    suggestion: str
    standard_ref: Optional[str] = None
    # ★ 修复可变默认值
    triggered_rules: List[str] = Field(default_factory=list)
    # 法规审核附加字段（Full 模式填充）
    law_references: Optional[List[str]] = None
    mandatory_measures: Optional[List[str]] = None
    inspection_advice: Optional[List[str]] = None


class FMEAEvaluationResult(BaseModel):
    """旧版完整评估结果模型"""

    report_summary: str = Field(..., description="报告摘要")
    defects: List[EvaluatedDefect] = Field(..., description="评估后的缺陷列表")