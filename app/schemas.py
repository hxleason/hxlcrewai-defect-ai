"""
app/schemas.py – Pydantic 模式定义（请求/响应模型） 终极版 v2.2

涵盖:
- Project 创建与输出
- Task 创建、输出（含监控字段）、详情输出（含缺陷列表）
- Defect 提取 / 更新 / 完整输出（新增 M‑1 字段 + M‑3/M‑4 标记）
- 内部流程模型（DefectExtractionResult、FMEAAnalysisResponse）

v2.2 更新：
- original_text 必须有默认值 ""，彻底解决 Field required 错误
- model_validator 补充 type→defect_type 映射 + 预填 original_text
- 添加 model_config extra='ignore' 忽略 LLM 输出的多余字段
- 适配 DeepSeek 等不支持 response_format 的 API
"""

from pydantic import BaseModel, Field, model_validator
from datetime import datetime
from typing import Optional, Dict, Any, List


# ══════════════════════════════════════════════════
# 一、Project 模式
# ══════════════════════════════════════════════════

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectOut(ProjectCreate):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════
# 二、Task 模式
# ══════════════════════════════════════════════════

class TaskCreate(BaseModel):
    project_id: Optional[int] = None
    type: str  # analysis / evaluation / full
    input_text: str


class TaskOut(BaseModel):
    id: int
    project_id: Optional[int] = None
    type: str
    status: str                   # pending / started / success / failure / pending_review
    progress: int
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    celery_task_id: Optional[str] = None
    created_at: datetime
    # 以下为任务监控新增字段
    started_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TaskDetailOut(TaskOut):
    """包含关联缺陷列表的任务详情"""
    defects: List["DefectOut"] = []


# ══════════════════════════════════════════════════
# 三、Defect 模式
# ══════════════════════════════════════════════════

class DefectBase(BaseModel):
    """缺陷提取公共字段（不含评估）"""
    defect_type: str = Field(..., description="缺陷类型")
    component: Optional[str] = None
    location: Optional[str] = None
    length: Optional[float] = None
    depth: Optional[float] = None
    unit: str = "mm"
    quantity: int = 1
    wall_thickness: Optional[float] = None
    original_text: str = ""                              # ★ 默认空字符串，缺失不报错
    # ---------- 新增字段（M‑1）----------
    detection_method: Optional[str] = None
    service_years: Optional[float] = None
    inspection_interval: Optional[str] = None

    # 允许额外字段（LLM 可能多输出 size、defect_id 等）
    model_config = {"extra": "ignore"}

    @model_validator(mode='before')
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        """
        预处理输入数据：
        - 将 'type' 字段自动映射为 'defect_type'
        - 若 'original_text' 缺失则补为空字符串
        """
        if isinstance(data, dict):
            # 别名映射
            if 'type' in data and 'defect_type' not in data:
                data['defect_type'] = data.pop('type')
            # 补充缺失的 original_text（虽然已有默认值，但二次保障）
            if 'original_text' not in data:
                data['original_text'] = ''
        return data


class DefectCreate(DefectBase):
    """用于创建新的缺陷记录（提取阶段）"""
    task_id: int     # 必须关联任务


class DefectUpdate(BaseModel):
    """用于更新缺陷的 FMEA 评估结果（部分更新）"""
    severity: Optional[int] = None
    occurrence: Optional[int] = None
    detection: Optional[int] = None
    rpn: Optional[int] = None
    risk_level: Optional[str] = None
    level: Optional[int] = None
    reasons: Optional[List[str]] = None
    suggestion: Optional[str] = None
    standard_ref: Optional[str] = None
    triggered_rules: Optional[List[str]] = None
    law_references: Optional[List[str]] = None
    mandatory_measures: Optional[List[str]] = None
    inspection_advice: Optional[List[str]] = None
    extra_data: Optional[Dict[str, Any]] = None
    # ---------- 新增字段（M‑3/M‑4）----------
    review_required: Optional[bool] = None
    ap: Optional[str] = None                     # H / M / L


class DefectOut(DefectBase):
    """缺陷完整输出（包含提取信息 + 评估结果 + 时间戳）"""
    id: int
    task_id: int
    # 评估字段（可为空，表示尚未评估）
    severity: Optional[int] = None
    occurrence: Optional[int] = None
    detection: Optional[int] = None
    rpn: Optional[int] = None
    risk_level: Optional[str] = None
    level: Optional[int] = None
    reasons: Optional[List[str]] = None
    suggestion: Optional[str] = None
    standard_ref: Optional[str] = None
    triggered_rules: Optional[List[str]] = None
    law_references: Optional[List[str]] = None
    mandatory_measures: Optional[List[str]] = None
    inspection_advice: Optional[List[str]] = None
    extra_data: Optional[Dict[str, Any]] = None
    # ---------- 新增字段 ----------
    review_required: Optional[bool] = None
    ap: Optional[str] = None
    # 时间戳
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# 解决 TaskDetailOut 的前向引用
TaskDetailOut.model_rebuild()


# ══════════════════════════════════════════════════
# 四、内部流程模型（供 crews.py 及 API 使用）
# ══════════════════════════════════════════════════

class DefectExtractionResult(BaseModel):
    """
    缺陷提取阶段输出（LLM 返回格式）。
    与 crews.py 配合使用，不再依赖 output_pydantic。
    """
    defects: List[DefectBase]


class FMEAAnalysisResponse(BaseModel):
    """
    FMEA 分析整体结果，可直接用于 API 返回。
    包含所有缺陷的完整评估及整体总结。
    """
    defects: List[DefectOut]
    summary: Optional[str] = None