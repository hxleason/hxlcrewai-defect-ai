# standards_indexer.py
import os
from langchain_community.document_loaders import TextLoader, DirectoryLoader
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

STANDARDS_DIR = "standards"
PERSIST_DIR = "./chroma_standards_db"

def main():
    if not os.path.isdir(STANDARDS_DIR):
        print(f"❌ 文件夹 '{STANDARDS_DIR}' 不存在")
        return

    print(f"📄 正在加载 {STANDARDS_DIR} 中的 .txt 文件...")
    loader = DirectoryLoader(
        STANDARDS_DIR,
        glob="**/*.txt",
        loader_cls=TextLoader,
        loader_kwargs={"autodetect_encoding": True}
    )
    docs = loader.load()
    if not docs:
        print("⚠️ 未找到任何文本文件，停止索引。")
        return

    print(f"🗂️ 共加载 {len(docs)} 个文档，正在分割...")
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500, chunk_overlap=50, separators=["\n\n", "\n", "。", ".", " "]
    )
    chunks = splitter.split_documents(docs)
    print(f"🔢 分割为 {len(chunks)} 个片段")

    embeddings = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese")
    print("🧠 正在创建向量库（可能需要几分钟）...")
    vs = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=PERSIST_DIR
    )
    vs.persist()
    print(f"✅ 索引完成，向量库已保存至 {PERSIST_DIR}")

if __name__ == "__main__":
    main()