# app/rag/vector_store.py
import os
import logging
from typing import Optional
from pathlib import Path

from celery.signals import worker_process_init
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

# 索引文件存放路径（优先取环境变量，否则使用默认值）
FAISS_INDEX_DIR = os.getenv("FAISS_INDEX_DIR", "app/rag/faiss_index")

# ✅ 全局变量，用于缓存预加载的向量库（Worker 进程使用）
_vector_store: Optional[FAISS] = None


def _get_local_model_path() -> str:
    """
    智能获取本地 Embedding 模型路径，兼容不同部署环境。
    优先级：环境变量 > 自动探测 'model/bge-small-zh-v1.5' > 'model' 目录 > 抛出异常。
    """
    # 1. 绝对环境变量（最优先）
    env_path = os.getenv("EMBEDDING_MODEL_PATH")
    if env_path and Path(env_path).exists():
        logger.info(f"使用环境变量指定的模型路径: {env_path}")
        return env_path

    # 2. 相对路径：<项目根>/model/bge-small-zh-v1.5
    base_dir = Path(__file__).resolve().parent.parent.parent  # 项目根目录
    candidate = base_dir / "model" / "bge-small-zh-v1.5"
    if candidate.exists():
        logger.info(f"使用自动探测的模型路径: {candidate}")
        return str(candidate)

    # 3. 如果模型直接放在 model 目录下（无子文件夹）
    candidate = base_dir / "model"
    if candidate.exists():
        logger.info(f"使用备选 model 目录: {candidate}")
        return str(candidate)

    # 4. 如果都不存在，抛出错误（或返回模型名要求联网，视需求修改）
    raise FileNotFoundError(
        f"❌ 找不到本地 Embedding 模型！请将模型放到 {base_dir / 'model'} 下，"
        f"或设置环境变量 EMBEDDING_MODEL_PATH"
    )


def load_vector_store() -> Optional[FAISS]:
    """
    加载预构建的 FAISS 向量库。
    如果索引文件不存在，则返回 None（你也可以在这里调用构建函数）
    """
    try:
        local_model_path = _get_local_model_path()
        logger.info(f"使用本地模型：{local_model_path}")

        embeddings = HuggingFaceEmbeddings(
            model_name=local_model_path,
            model_kwargs={'device': 'cpu'},
            encode_kwargs={'normalize_embeddings': True},
        )

        vector_store = FAISS.load_local(
            FAISS_INDEX_DIR,
            embeddings,
            allow_dangerous_deserialization=True
        )
        logger.info(f"✅ 向量库加载成功，共 {vector_store.index.ntotal} 条记录")
        return vector_store
    except Exception as e:
        logger.error(f"❌ 向量库加载失败: {e}")
        return None


# ✅ Worker 进程启动时的预加载钩子
@worker_process_init.connect
def init_worker_vector_store(**kwargs):
    """
    每个 Celery Worker 子进程启动时自动调用。
    将向量库加载到全局变量，后续所有任务直接复用。
    """
    global _vector_store
    logger.info("🔄 Worker 初始化：开始预加载向量库...")
    _vector_store = load_vector_store()
    if _vector_store is not None:
        logger.info("✅ Worker 向量库预加载完成")
    else:
        logger.warning("⚠️ Worker 向量库预加载失败，后续任务将尝试懒加载")


# ✅ 统一获取向量库的接口
def get_vector_store() -> Optional[FAISS]:
    """
    获取向量库实例。
    - 若在 Celery Worker 中，优先返回预加载的全局变量；
    - 若全局变量为空（非 Worker 环境或预加载失败），则实时懒加载。
    """
    global _vector_store
    if _vector_store is not None:
        return _vector_store
    else:
        logger.info("⚡ 全局向量库为空，执行懒加载...")
        return load_vector_store()