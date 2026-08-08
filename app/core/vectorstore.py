"""
app/core/vectorstore.py - 标准向量库管理（环境变量驱动）

功能：
    - 从指定目录加载 .txt 标准文档，生成向量库
    - 持久化到 Chroma，支持重启后复用
    - 提供 search_standards 接口供 Agent 检索

所有路径均通过环境变量读取，无外部配置文件依赖。
"""

import os
import logging
from typing import List, Dict, Optional

# 依赖处理（兼容不同 langchain 版本）
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    from langchain_huggingface import HuggingFaceEmbeddings
except ImportError:
    from langchain_community.embeddings import HuggingFaceEmbeddings

try:
    from langchain_chroma import Chroma
except ImportError:
    from langchain_community.vectorstores import Chroma

logger = logging.getLogger("defect_fmea.vectorstore")

# 环境变量读取（提供默认值）
STANDARDS_FOLDER   = os.getenv("STANDARDS_FOLDER",   "app/data/standards")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "app/data/chroma_db")

_vectorstore: Optional[Chroma] = None


def get_vectorstore(
    persist_dir: Optional[str] = None,
    standards_dir: Optional[str] = None,
) -> Optional[Chroma]:
    """
    获取全局单例向量存储。
    优先加载持久化过的 Chroma；若不存在则从文档目录创建。
    """
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    persist_dir = os.path.abspath(persist_dir or CHROMA_PERSIST_DIR)
    standards_dir = os.path.abspath(standards_dir or STANDARDS_FOLDER)

    # 1. 尝试加载已持久化的向量库
    if os.path.exists(persist_dir) and os.listdir(persist_dir):
        try:
            embeddings = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese")
            _vectorstore = Chroma(persist_directory=persist_dir, embedding_function=embeddings)
            logger.info(f"✅ 已从 {persist_dir} 加载持久化向量库")
            return _vectorstore
        except Exception as e:
            logger.warning(f"⚠️ 加载持久化向量库失败，将重新创建: {e}")

    # 2. 检查文档目录
    if not os.path.isdir(standards_dir):
        logger.warning(f"❌ 标准文档目录不存在: {standards_dir}")
        return None

    # 3. 加载 .txt 文档
    loader = DirectoryLoader(
        standards_dir,
        glob="**/*.txt",
        loader_cls=TextLoader,
        loader_kwargs={"autodetect_encoding": True}
    )
    try:
        docs = loader.load()
    except Exception as e:
        logger.error(f"❌ 文档加载失败: {e}")
        return None

    if not docs:
        logger.warning("📂 文档目录为空，无法创建向量库")
        return None

    # 4. 文本切分
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n\n", "\n", "。", ".", " "]
    )
    chunks = text_splitter.split_documents(docs)
    logger.info(f"📄 已切分 {len(docs)} 个文档 → {len(chunks)} 个片段")

    # 5. 生成向量并持久化
    embeddings = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese")
    try:
        _vectorstore = Chroma.from_documents(
            documents=chunks,
            embedding=embeddings,
            persist_directory=persist_dir,
        )
        logger.info(f"✅ 向量库创建完成，存储于 {persist_dir}（片段数 {len(chunks)}）")
    except Exception as e:
        logger.error(f"❌ 向量库创建失败: {e}")
        return None

    return _vectorstore


def search_standards(query: str, k: int = 3) -> List[Dict[str, str]]:
    """
    搜索与查询最相关的标准文档片段。
    """
    vs = get_vectorstore()
    if vs is None:
        logger.warning("⚠️ 向量库未就绪，返回占位信息")
        return [{"content": "标准向量库未配置或启动失败，请检查 STANDARDS_FOLDER 与 CHROMA_PERSIST_DIR 环境变量。", "source": ""}]

    try:
        docs = vs.similarity_search(query, k=k)
    except Exception as e:
        logger.error(f"❌ 相似度搜索失败: {e}")
        return [{"content": f"搜索异常: {e}", "source": ""}]

    return [
        {
            "content": doc.page_content,
            "source": os.path.basename(doc.metadata.get("source", "")),
        }
        for doc in docs
    ]


def preload_vectorstore():
    """应用启动时预加载向量库，避免首次请求卡顿。"""
    get_vectorstore()
    logger.info("🚀 向量库预加载完成")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    preload_vectorstore()
    results = search_standards("管道焊缝裂纹验收标准")
    for i, res in enumerate(results, 1):
        print(f"\n--- 结果 {i} (来源: {res['source']}) ---")
        print(res["content"][:200])