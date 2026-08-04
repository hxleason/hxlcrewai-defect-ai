import os
import logging
from typing import List, Dict, Optional
from langchain_community.document_loaders import TextLoader, DirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
try:
    from langchain_huggingface import HuggingFaceEmbeddings
except ImportError:
    from langchain_community.embeddings import HuggingFaceEmbeddings
try:
    from langchain_chroma import Chroma
except ImportError:
    from langchain_community.vectorstores import Chroma

from app.config import STANDARDS_FOLDER, CHROMA_PERSIST_DIR

logger = logging.getLogger("defect_fmea.vectorstore")
_vectorstore: Optional[Chroma] = None

def get_vectorstore(
    persist_dir: str = CHROMA_PERSIST_DIR,
    standards_dir: str = STANDARDS_FOLDER,
) -> Optional[Chroma]:
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    if os.path.exists(persist_dir) and os.listdir(persist_dir):
        try:
            embeddings = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese")
            _vectorstore = Chroma(persist_directory=persist_dir, embedding_function=embeddings)
            logger.info(f"✅ 已加载持久化标准向量库于 {persist_dir}")
            return _vectorstore
        except Exception as e:
            logger.warning(f"加载持久化向量库失败，将重新创建：{e}")

    if not os.path.isdir(standards_dir):
        logger.warning(f"标准文件夹 '{standards_dir}' 不存在，无法创建向量库。")
        return None

    loader = DirectoryLoader(
        standards_dir,
        glob="**/*.txt",
        loader_cls=TextLoader,
        loader_kwargs={"autodetect_encoding": True}
    )
    docs = loader.load()
    if not docs:
        return None

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500, chunk_overlap=50,
        separators=["\n\n", "\n", "。", ".", " "]
    )
    chunks = text_splitter.split_documents(docs)

    embeddings = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese")
    try:
        _vectorstore = Chroma.from_documents(
            documents=chunks,
            embedding=embeddings,
            persist_directory=persist_dir,
        )
        _vectorstore.persist()
        logger.info(f"✅ 标准向量库已创建，共 {len(chunks)} 个片段，存储于 {persist_dir}")
    except Exception as e:
        logger.error(f"创建向量库失败：{e}")
        return None
    return _vectorstore

def search_standards(query: str, k: int = 3) -> List[Dict[str, str]]:
    vs = get_vectorstore()
    if vs is None:
        return [{"content": "标准向量库未就绪，无法搜索", "source": ""}]
    docs = vs.similarity_search(query, k=k)
    return [
        {
            "content": doc.page_content,
            "source": os.path.basename(doc.metadata.get("source", ""))
        }
        for doc in docs
    ]