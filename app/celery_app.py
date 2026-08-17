"""
Celery 配置（终极版）
- 支持 Windows / Linux 双平台
- 线程池（Windows）或进程池（Linux）以保障 group / chord 并行执行
- Broker 与 Backend 统一从 Settings 读取，便于多环境部署
- 定时任务：每 5 分钟检查卡住的任务
- 向量库预加载 + 就绪提示
"""
import os
import sys
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready
from dotenv import load_dotenv

from app.core.config import settings

load_dotenv()  # 双重保险，确保 .env 已加载

# ── 创建 Celery 实例（配置来源：settings） ──
celery_app = Celery(
    "fmea_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.analysis",
        "app.tasks.scheduler",
    ],
)

# ── 基础配置 ──
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600 * 24 * 7,
    # 日志（避免 Windows 下 ANSI 闪屏）
    worker_log_format="%(asctime)s [%(levelname)s] %(message)s",
    worker_task_log_format="%(asctime)s [%(levelname)s] %(task_name)s: %(message)s",
    worker_log_color=False,
)

# ── 并发策略（保证新版流水线 group / chord 能真正并行）──
if sys.platform == "win32":
    # Windows 无法使用 prefork，选用 threads 池
    celery_app.conf.worker_pool = "threads"
    celery_app.conf.worker_concurrency = 4          # 根据实际 CPU 核心数调整
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")   # 避免某些库报错
else:
    # Linux / macOS 推荐 prefork（多进程），也可改用 threads
    celery_app.conf.worker_pool = "prefork"
    celery_app.conf.worker_concurrency = 4          # 生产环境可调高至 8~16

# ── Beat 调度 ──
celery_app.conf.beat_schedule = {
    "check-stuck-tasks-every-5-minutes": {
        "task": "app.tasks.scheduler.check_stuck_tasks",
        "schedule": crontab(minute="*/5"),
    },
}

# ── 向量库预加载 ──
# 每个 worker 启动时自动加载 FAISS 索引，避免首次任务阻塞
from app.rag import vector_store  # noqa: E402,F401

# ── 就绪提示 ──
@worker_ready.connect
def on_worker_ready(**kwargs):
    """主进程就绪后打印一次配置信息"""
    print("✅ Celery Worker 已就绪", flush=True)
    print(f"🔧 池类型: {celery_app.conf.worker_pool or '默认'}", flush=True)
    print(f"🔧 并发数: {celery_app.conf.worker_concurrency}", flush=True)
    print(f"🔧 向量索引目录: {os.getenv('FAISS_INDEX_DIR', '未设置')}", flush=True)
    print(f"🔧 平台: {sys.platform}", flush=True)