"""
法规文档向量索引构建脚本（使用本地模型，无需联网）
运行方式：python -m app.rag.build_index
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中（防止直接运行脚本时找不到 app 包）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------- 第三方库 ----------------
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS

# ---------------- 项目内部 ----------------
from app.core.config import settings, FAISS_INDEX_PATH, REGULATIONS_DOC_PATH
from app.rag.vector_store import _get_local_model_path  # 复用统一的模型路径解析


def build_index():
    # 1. 检查法规文档是否存在
    if not REGULATIONS_DOC_PATH.exists():
        raise FileNotFoundError(f"法规文档未找到: {REGULATIONS_DOC_PATH}")

    print(f"📄 加载文档: {REGULATIONS_DOC_PATH}")
    loader = TextLoader(str(REGULATIONS_DOC_PATH), encoding="utf-8")
    documents = loader.load()
    print(f"✅ 成功加载，共 {len(documents)} 个文档")

    # 2. 文本分割（参数全部取自 settings）
    chunk_size = settings.CHUNK_SIZE
    chunk_overlap = settings.CHUNK_OVERLAP
    print(f"✂️ 文档分块 (chunk_size={chunk_size}, overlap={chunk_overlap})")
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        # 中文分句友好分隔符
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]
    )
    chunks = text_splitter.split_documents(documents)
    print(f"✅ 分割完成，共 {len(chunks)} 个文本块")

    # 3. 通过统一函数获取本地嵌入模型路径（不再硬编码）
    local_model_path = _get_local_model_path()
    print(f"🧠 使用本地嵌入模型: {local_model_path}")

    embeddings = HuggingFaceEmbeddings(
        model_name=local_model_path,          # 函数返回 str，可直接使用
        model_kwargs={'device': 'cpu'},       # 有 GPU 可改为 'cuda'
        encode_kwargs={'normalize_embeddings': True}
    )

    # 4. 构建 FAISS 向量库
    print("🔄 构建向量索引...")
    vectorstore = FAISS.from_documents(chunks, embeddings)
    print("✅ 向量索引构建完成")

    # 5. 持久化索引到磁盘
    FAISS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"💾 保存索引至: {FAISS_INDEX_PATH}")
    vectorstore.save_local(str(FAISS_INDEX_PATH))
    print("🎉 全部完成！法规索引已就绪。")


if __name__ == "__main__":
    build_index()