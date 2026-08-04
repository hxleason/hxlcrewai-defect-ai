"""
法规与标准知识库模块
- 结构化法规条目：从 regulations/*.txt 加载 JSON 行，支持按缺陷类型+等级精确查询
- 标准全文向量库：加载 standards/ 目录下的 .txt/.pdf 文件（当前仅支持 .txt），
  使用 Chroma + HuggingFaceEmbedding 提供语义搜索
"""
import os
import re
import json
from typing import List, Dict, Optional

from langchain_community.document_loaders import TextLoader, DirectoryLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

# ========== 第一部分：结构化法规索引 ==========
REGULATION_INDEX: Dict[str, Dict[int, dict]] = {}
DEFAULT_ENTRY = {
    "law_references": "暂未收录",
    "mandatory_measures": "按企业标准执行",
    "inspection_advice": "请咨询总工程师"
}

def load_structured_regulations(directory: str = "regulations") -> int:
    """加载 regulations/ 下每行一条 JSON 的法规条目，构建类型+等级索引。"""
    global REGULATION_INDEX
    REGULATION_INDEX.clear()
    count = 0

    if not os.path.isdir(directory):
        print(f"⚠️ 结构化法规目录 {directory} 不存在，使用默认兜底信息。")
        return 0

    for filename in os.listdir(directory):
        if not filename.endswith(".txt"):
            continue
        filepath = os.path.join(directory, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    defect_type = record.get("defect_type")
                    level = record.get("level")
                    if not defect_type or level is None:
                        continue

                    defect_type = defect_type.strip()
                    try:
                        level = int(level)
                    except (ValueError, TypeError):
                        continue

                    if defect_type not in REGULATION_INDEX:
                        REGULATION_INDEX[defect_type] = {}
                    REGULATION_INDEX[defect_type][level] = {
                        "law_references": record.get("law_references", "无"),
                        "mandatory_measures": record.get("mandatory_measures", "无"),
                        "inspection_advice": record.get("inspection_advice", "无")
                    }
                    count += 1
        except Exception as e:
            print(f"❌ 读取结构化文件 {filename} 失败: {e}")

    print(f"✅ 结构化法规索引加载完成，共 {count} 条记录。")
    return count

# ========== 第二部分：标准全文向量库（用于 GB/T 26610 等） ==========
_vectorstore = None

def get_vectorstore(persist_dir: str = "./chroma_standards_db", 
                    standards_dir: str = "standards") -> Chroma:
    """初始化或加载持久化的向量库，索引 standards/ 目录下所有 .txt 文件。"""
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    # 如果持久化目录已存在，直接加载
    if os.path.exists(persist_dir) and os.listdir(persist_dir):
        from chromadb.config import Settings
        embeddings = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese")
        _vectorstore = Chroma(persist_directory=persist_dir, 
                              embedding_function=embeddings)
        print(f"✅ 从 {persist_dir} 加载了向量库。")
        return _vectorstore

    # 否则，新建向量库
    if not os.path.isdir(standards_dir):
        print(f"⚠️ 标准目录 {standards_dir} 不存在，无法创建向量库。")
        return None

    loader = DirectoryLoader(
        standards_dir,
        glob="**/*.txt",
        loader_cls=TextLoader,
        loader_kwargs={"autodetect_encoding": True}
    )
    docs = loader.load()
    if not docs:
        print(f"⚠️ 标准目录 {standards_dir} 中没有找到任何 .txt 文件。")
        return None

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n\n", "\n", "。", ".", " "]
    )
    chunks = text_splitter.split_documents(docs)

    embeddings = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese")
    _vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=persist_dir
    )
    _vectorstore.persist()
    print(f"✅ 标准向量库已创建，共 {len(chunks)} 个片段，持久化于 {persist_dir}")
    return _vectorstore

def search_standards(query: str, k: int = 3) -> List[Dict[str, str]]:
    """语义搜索标准全文，返回相关片段及来源。"""
    vs = get_vectorstore()
    if vs is None:
        return [{"content": "标准向量库未就绪，无法搜索。", "source": ""}]
    docs = vs.similarity_search(query, k=k)
    results = []
    for doc in docs:
        results.append({
            "content": doc.page_content,
            "source": os.path.basename(doc.metadata.get("source", ""))
        })
    return results

# ========== 第三部分：统一查询接口（解析意图后路由） ==========
def _parse_query_for_defect_level(query: str) -> tuple[Optional[str], Optional[int]]:
    """从查询中提取缺陷类型和风险等级，如'局部减薄 等级2'"""
    match = re.search(r"等级\s*(\d+)", query) or re.search(r"level\s*(\d+)", query, re.IGNORECASE)
    if not match:
        return None, None
    level = int(match.group(1))
    prefix = query[:match.start()].strip()
    defect_type = re.sub(r"\s*(等级|level|处理措施|怎么|如何|要求).*", "", prefix, flags=re.IGNORECASE).strip()
    return defect_type if defect_type else None, level

def search_regulations(query: str, k: int = 3, prefer_structured: bool = True) -> List[Dict[str, str]]:
    """
    统一的法规/标准查询入口。
    - 如果查询中包含明确的缺陷类型+等级，优先返回结构化法规条目（单个结果包装为列表）。
    - 否则使用向量库进行标准全文检索。
    - 返回格式统一为列表，每个元素包含 content 和 source 字段。
    """
    # 1. 尝试结构化精确匹配
    if REGULATION_INDEX:
        defect_type, level = _parse_query_for_defect_level(query)
        if defect_type and level is not None:
            record = REGULATION_INDEX.get(defect_type, {}).get(level)
            if not record:
                # 模糊匹配一下（如输入"筒体局部减薄"）
                for t, levels in REGULATION_INDEX.items():
                    if t in defect_type or defect_type in t:
                        record = levels.get(level)
                        if record:
                            break
            if record:
                # 将结构化条目转为文本段落，便于展示
                content_text = (
                    f"【缺陷处理】{defect_type} 等级{level}\n"
                    f"法规依据: {record['law_references']}\n"
                    f"强制措施: {record['mandatory_measures']}\n"
                    f"检测建议: {record['inspection_advice']}"
                )
                return [{
                    "content": content_text,
                    "source": f"regulations/{defect_type}.txt"
                }]

    # 2. 如果没有匹配到结构化条目，则使用向量库进行语义搜索
    if prefer_structured is False or (not _parse_query_for_defect_level(query)[0]):
        return search_standards(query, k=k)

    # 3. 如果用户明确要求结构化但未匹配，返回空或提示
    return [{"content": "未找到对应的结构化法规条目，请尝试使用标准全文检索。", "source": ""}]

# ========== 启动时自动加载 ==========
load_structured_regulations()
# 向量库采用懒加载，避免启动时卡顿，首次调用时初始化

if __name__ == "__main__":
    # 测试结构化检索
    print("\n>>> 结构化检索测试：")
    res = search_regulations("局部减薄 等级2 处理措施")
    for item in res:
        print(f"  [{item['source']}] {item['content']}\n")

    # 测试标准全文向量检索
    print("\n>>> 标准全文检索测试：")
    res = search_regulations("超声检测灵敏度要求", prefer_structured=False)
    for item in res:
        print(f"  [{item['source']}] {item['content']}\n")