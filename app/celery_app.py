# app/celery_app.py
import os
import sys

# ================= 0. 加载 .env 环境变量（必须在所有导入之前） =================
from dotenv import load_dotenv
load_dotenv()  # 自动读取项目根目录的 .env 文件

from celery import Celery
from celery.signals import worker_ready  # ← 新增：只让主进程运行一次

# ================= 1. 创建 Celery 实例 =================
celery_app = Celery(
    "fmea_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    include=[
        "app.tasks.analysis",       # 你的异步分析任务
        # 如果之前添加了 test_env 任务，可以取消下面这行注释：
        # "app.tasks.test_env",
    ]
)

# ================= 2. 生产级配置 =================
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
)

# ================= 3. 触发向量库预加载 =================
# 导入 vector_store 模块，其内部的 @worker_process_init.connect 会在每个 Worker 子进程启动时自动执行
# 从而将 FAISS 向量库一次性加载到全局变量，后续任务直接复用
from app.rag import vector_store  # noqa: E402,F401

# ================= 4. 主进程就绪后打印一次诊断信息 =================
@worker_ready.connect
def on_worker_ready(**kwargs):
    """
    当 Celery 主进程完全就绪后触发（仅调用一次）。
    用于打印关键环境变量和状态，方便排查。
    """
    faiss_dir = os.getenv("FAISS_INDEX_DIR", "app/rag/faiss_index")
    model_path = os.getenv("EMBEDDING_MODEL_PATH", "自动探测")
    print("✅ [celery_app] 配置完成，向量库预加载信号已注册", file=sys.stderr, flush=True)
    print(
        f"🔧 [celery_app] 关键环境变量: FAISS_INDEX_DIR={faiss_dir}, EMBEDDING_MODEL_PATH={model_path}",
        file=sys.stderr, flush=True
    )