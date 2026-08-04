# app/rag/vector_store.py
import os
import logging
from typing import Optional

from celery.signals import worker_process_init           # ✅ 新增导入

from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

# 索引文件存放路径（可配置）
FAISS_INDEX_DIR = os.getenv("FAISS_INDEX_DIR", "app/rag/faiss_index")

# ✅ 全局变量，用于缓存预加载的向量库（Worker 进程使用）
_vector_store: Optional[FAISS] = None


def load_vector_store() -> Optional[FAISS]:
    """
    加载预构建的 FAISS 向量库。
    如果索引文件不存在，则返回 None（你也可以在这里调用构建函数）
    """
    try:
        embeddings = HuggingFaceEmbeddings(
            model_name="BAAI/bge-small-zh-v1.5",
            model_kwargs={'device': 'cpu'},
            encode_kwargs={'normalize_embeddings': True}
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