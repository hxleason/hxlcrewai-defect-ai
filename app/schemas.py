# app/db/schemas.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Dict, Any


# ---------- 原有 Project Schema ----------
class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectOut(ProjectCreate):
    id: int
    created_at: datetime

    model_config = {
        "from_attributes": True   # Pydantic V2 替代 orm_mode
    }


# ---------- 新增 Task Schema ----------
class TaskCreate(BaseModel):
    project_id: Optional[int] = None
    type: str                     # analysis / evaluation / full
    input_text: str


class TaskOut(BaseModel):
    id: int
    project_id: Optional[int] = None
    type: str
    status: str                   # pending / started / success / failure
    progress: int
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    celery_task_id: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    model_config = {
        "from_attributes": True
    }