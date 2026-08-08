from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

class FMEAItemBase(BaseModel):
    project_id: int
    component: str
    function: str
    failure_mode: str
    effect: str
    severity: int = Field(..., ge=1, le=10)
    cause: Optional[str] = None
    occurrence: Optional[int] = Field(None, ge=1, le=10)
    current_controls: Optional[str] = None
    detection: Optional[int] = Field(None, ge=1, le=10)
    rpn: Optional[int] = None

class FMEAItemCreate(FMEAItemBase):
    pass

class FMEAItemUpdate(BaseModel):
    component: Optional[str] = None
    function: Optional[str] = None
    failure_mode: Optional[str] = None
    effect: Optional[str] = None
    severity: Optional[int] = Field(None, ge=1, le=10)
    cause: Optional[str] = None
    occurrence: Optional[int] = Field(None, ge=1, le=10)
    current_controls: Optional[str] = None
    detection: Optional[int] = Field(None, ge=1, le=10)
    rpn: Optional[int] = None

class FMEAItemInDB(FMEAItemBase):
    id: int
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        orm_mode = True