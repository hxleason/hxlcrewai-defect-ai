# ==================== app/rag/build_index.py ====================
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

# ✅ 使用新版 langchain-huggingface 中的 HuggingFaceEmbeddings（避免弃用警告）
from langchain_huggingface import HuggingFaceEmbeddings

from langchain_community.vectorstores import FAISS

# ---------------- 项目内部 ----------------
from app.core.config import settings, FAISS_INDEX_PATH, REGULATIONS_DOC_PATH


# ✅ 硬编码你的本地模型路径（请根据实际情况检查）
LOCAL_MODEL_PATH = Path(r"C:\hugging镜像\bge-small-zh-v1.5")


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

    # 3. ✅ 初始化本地嵌入模型（不再依赖 settings.EMBEDDING_MODEL_NAME）
    if not LOCAL_MODEL_PATH.exists():
        raise FileNotFoundError(
            f"本地模型文件夹不存在: {LOCAL_MODEL_PATH}\n"
            f"请确保已将模型复制到该路径，或修改代码中的 LOCAL_MODEL_PATH 变量。"
        )

    print(f"🧠 使用本地嵌入模型: {LOCAL_MODEL_PATH}")
    embeddings = HuggingFaceEmbeddings(
        model_name=str(LOCAL_MODEL_PATH),          # 必须转为字符串
        model_kwargs={'device': 'cpu'},            # 有 GPU 可改为 'cuda'
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