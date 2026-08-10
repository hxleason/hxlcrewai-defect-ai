"""
RAG 向量存储模块 – FAISS 专用（安全加固版 + 线程安全单例）
========================================================
负责加载预构建的 FAISS 本地向量索引，并提供统一的检索接口。

🔒 安全声明：
- FAISS 索引文件内部使用 Pickle 序列化，加载时需传递
  `allow_dangerous_deserialization=True`，存在代码执行风险。
- 为避免意外加载不可信的外部索引，本站点要求**必须设置环境变量
  ALLOW_FAISS_DANGEROUS_DESERIALIZATION=1** 才能启动，以明确表示
  您已确认索引文件来源可信。
- 部署时请保证 FAISS 索引目录仅由本系统写入，严禁接收用户上传的索引文件。

🧵 线程安全：
- 使用 threading.Lock 保护全局单例的初始化，避免多线程竞态条件。
- 支持 Celery Worker 预加载，且与懒加载无缝协作。
"""

import os
import logging
import threading
from typing import Optional
from pathlib import Path

from celery.signals import worker_process_init
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

# 索引文件存放路径（优先取环境变量，否则使用默认值）
FAISS_INDEX_DIR = os.getenv("FAISS_INDEX_DIR", "app/rag/faiss_index")

# ── 线程安全单例组件 ──
_vector_store: Optional[FAISS] = None          # 缓存的向量库实例
_vector_store_lock = threading.Lock()          # 保护 _vector_store 的锁


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

    # 4. 如果都不存在，抛出错误
    raise FileNotFoundError(
        f"❌ 找不到本地 Embedding 模型！请将模型放到 {base_dir / 'model'} 下，"
        f"或设置环境变量 EMBEDDING_MODEL_PATH"
    )


def load_vector_store() -> Optional[FAISS]:
    """
    加载预构建的 FAISS 向量库（已加固安全校验）。
    需要先通过环境变量 ALLOW_FAISS_DANGEROUS_DESERIALIZATION 授权。
    如果索引文件不存在，则返回 None。
    """
    # 安全锁：必须显式设置环境变量
    if os.getenv("ALLOW_FAISS_DANGEROUS_DESERIALIZATION") != "1":
        logger.critical(
            "🔒 安全限制：FAISS 加载使用了危险的 Pickle 反序列化。\n"
            "   如果您已确认索引文件完全受信，请在环境变量中设置：\n"
            "   export ALLOW_FAISS_DANGEROUS_DESERIALIZATION=1\n"
            "   并重启应用。"
        )
        raise RuntimeError(
            "FAISS 加载未授权！请设置 ALLOW_FAISS_DANGEROUS_DESERIALIZATION=1"
        )

    try:
        local_model_path = _get_local_model_path()
        logger.info(f"使用本地模型：{local_model_path}")

        embeddings = HuggingFaceEmbeddings(
            model_name=local_model_path,
            model_kwargs={'device': 'cpu'},
            encode_kwargs={'normalize_embeddings': True},
        )

        # 检查索引目录是否存在
        if not os.path.exists(FAISS_INDEX_DIR) or not os.path.isdir(FAISS_INDEX_DIR):
            logger.error(f"❌ FAISS 索引目录不存在: {FAISS_INDEX_DIR}")
            return None

        vector_store = FAISS.load_local(
            FAISS_INDEX_DIR,
            embeddings,
            allow_dangerous_deserialization=True
        )
        logger.info(f"✅ 向量库加载成功，共 {vector_store.index.ntotal} 条记录")
        return vector_store
    except FileNotFoundError as e:
        logger.error(f"❌ 模型或索引文件缺失: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ 向量库加载失败: {e}")
        return None


# ✅ 统一获取向量库的接口（线程安全，双重检查锁定）
def get_vector_store() -> Optional[FAISS]:
    """
    获取向量库实例（线程安全）。
    1. 优先返回已缓存的有效实例。
    2. 若无缓存，加锁后再次检查，避免多线程重复初始化。
    3. 调用 load_vector_store() 加载，并将结果缓存。
    """
    global _vector_store

    # 第一次检查（无锁，用于快速路径）
    if _vector_store is not None:
        return _vector_store

    # 进入临界区
    with _vector_store_lock:
        # 第二次检查（持有锁，确保只初始化一次）
        if _vector_store is None:
            logger.info("⚡ 全局向量库为空，执行线程安全的懒加载...")
            _vector_store = load_vector_store()
            if _vector_store is not None:
                logger.info("✅ 向量库懒加载成功")
            else:
                logger.warning("⚠️ 向量库懒加载失败，后续请求将返回 None")
        return _vector_store


# ✅ Worker 进程启动时的预加载钩子
@worker_process_init.connect
def init_worker_vector_store(**kwargs):
    """
    每个 Celery Worker 子进程启动时自动调用。
    通过线程安全的方式将向量库加载到全局缓存中，后续所有任务直接复用。
    """
    # Worker 进程初始化时通常只有单线程，但为安全仍通过 get_vector_store 加锁加载
    logger.info("🔄 Worker 初始化：开始预加载向量库...")
    store = get_vector_store()
    if store is not None:
        logger.info("✅ Worker 向量库预加载完成")
    else:
        logger.warning("⚠️ Worker 向量库预加载失败，后续任务将返回 None")