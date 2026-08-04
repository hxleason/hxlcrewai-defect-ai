# app/celery_app.py
import sys
from celery import Celery

# ---------- 1. 创建 Celery 实例 ----------
celery_app = Celery(
    "fmea_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    # 自动发现任务模块（根据你的实际任务文件路径调整）
    include=[
        "app.tasks.analysis",          # 你的异步任务定义
        # "app.tasks.other_tasks",     # 可继续添加其他任务模块
    ]
)

# ---------- 2. 生产级配置 ----------
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,

    # 任务管理高级配置（可选，推荐开启）
    task_track_started=True,           # 支持 "STARTED" 状态
    task_acks_late=True,               # 任务执行完再确认，防丢失
    worker_prefetch_multiplier=1,      # 一次只取一个任务（适合长耗时任务）
    result_expires=3600 * 24 * 7,      # 任务结果在 Redis 中保留 7 天
)

# ---------- 3. 触发向量库预加载 ----------
# 导入 vector_store 模块，其内部的 @worker_process_init.connect 会在每个 Worker 子进程启动时自动执行
# 从而将 FAISS 向量库一次性加载到全局变量，后续任务直接复用
from app.rag import vector_store  # noqa: E402,F401

# ---------- 4. 可选：诊断打印 ----------
print("✅ [celery_app] 配置完成，向量库预加载信号已注册", file=sys.stderr, flush=True)