# rebuild_faiss.py - rebuild data/faiss_index from the local knowledge base
import json, os, shutil
from pathlib import Path

MODEL_PATH = r"models\bge-small-zh-v1.5"
OUT_DIR    = r"data\faiss_index"
SOURCES    = [r"data\expert_rules.json", r"data\failure_cases.json"]
CHUNK_SIZE, CHUNK_OVERLAP = 400, 50

try:
    from langchain_huggingface import HuggingFaceEmbeddings
except ImportError:
    from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.schema import Document

def records_to_documents(path):
    data = json.load(open(path, encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    docs = []
    for i, rec in enumerate(records):
        text = json.dumps(rec, ensure_ascii=False)
        if len(text) <= CHUNK_SIZE:
            docs.append(Document(page_content=text, metadata={"source": str(Path(path)), "idx": i}))
        else:
            for j in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP):
                docs.append(Document(page_content=text[j:j+CHUNK_SIZE],
                                     metadata={"source": str(Path(path)), "idx": i, "chunk": j}))
    return docs

def main():
    docs = []
    for s in SOURCES:
        docs += records_to_documents(s)
    print(f"total documents: {len(docs)}")

    emb = HuggingFaceEmbeddings(model_name=MODEL_PATH)
    store = FAISS.from_documents(docs, emb)

    bdir = OUT_DIR + "_backup_1vec"
    if os.path.exists(bdir):
        shutil.rmtree(bdir)
    if os.path.exists(OUT_DIR):
        shutil.move(OUT_DIR, bdir)          # keep the old 1-vector index as backup
    os.makedirs(OUT_DIR, exist_ok=True)
    store.save_local(OUT_DIR)
    print("saved:", OUT_DIR)

if __name__ == "__main__":
    main()
