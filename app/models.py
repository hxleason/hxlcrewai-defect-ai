from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.db.base import Base


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