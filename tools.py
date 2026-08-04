# tools.py
import json
import os
import re
from typing import List, Dict, Optional

# ---------- 兼容不同的 crewai 版本 ----------
try:
    from crewai.tools import tool
except ImportError:
    class MockTool:
        def __init__(self, name, description, func):
            self.name = name
            self.description = description
            self.func = func
        def __call__(self, *args, **kwargs):
            return self.func(*args, **kwargs)

    def tool(name=None, description=None):
        def decorator(func):
            tool_name = name or func.__name__
            tool_desc = description or func.__doc__ or ""
            return MockTool(tool_name, tool_desc, func)
        return decorator

# ---------- 确定性计算器 ----------
from defect_grader import calc_fmea, diagnose_causes

# ------------------------- 内部辅助函数：法规库加载 -------------------------
REGULATIONS_FILE = os.path.join(os.path.dirname(__file__), "regulations", "regulations.json")

def _load_regulation_db() -> dict:
    """加载结构化法规库（JSON格式）。若文件不存在则返回空字典。"""
    if not os.path.isfile(REGULATIONS_FILE):
        return {}
    try:
        with open(REGULATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# ===================== 诊断工具 =====================
@tool("diagnosis_tool")
def diagnosis_tool(defect_type: str, quantity: int = 1,
                   length_mm: float = 0, depth_mm: float = 0) -> str:
    """
    根据缺陷类型查询可能原因列表，返回 JSON 字符串。
    参数 defect_type 为必填，其余参数为可选（仅用于未来扩展）。
    示例输入：defect_type='裂纹'
    输出：["原因1", "原因2"]
    """
    try:
        causes = diagnose_causes(defect_type)
        return json.dumps(causes, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"诊断失败: {str(e)}"}, ensure_ascii=False)


# ===================== 风险评估工具 =====================
@tool("risk_assessment_tool")
def risk_assessment_tool(
    defect_type: str,
    quantity: int = 1,
    length_mm: float = 0,
    depth_mm: float = 0,
    diameter_mm: float = 0,
    density: str = "分散",
    wall_thickness: Optional[float] = None    # 改为 None，避免默认值误导
) -> str:
    """
    根据缺陷信息计算 S/O/D/RPN 及安全等级，返回 JSON。
    核心参数说明：
    - defect_type: 缺陷类型（如裂纹、气孔、针孔等），必填
    - length_mm: 裂纹长度(mm)，仅当缺陷为裂纹时有效
    - depth_mm: 裂纹深度(mm)，仅当缺陷为裂纹时有效
    - diameter_mm: 气孔/针孔直径(mm)，仅当缺陷为气孔/针孔时有效
    - density: 气孔密度（'密集' 或 '分散'），默认'分散'
    - wall_thickness: 壁厚(mm)，若为 None 则表示未提供，将直接返回无法评定

    返回 JSON 包含字段：severity, occurrence, detection, rpn, risk_level, level, standard_ref
    若 wall_thickness 缺失，则返回 level=0 且 severities 等均为0。
    """
    # 1. 若没有有效壁厚，直接返回无法评定
    if wall_thickness is None or wall_thickness <= 0:
        return json.dumps({
            "error": "缺少有效壁厚参数，无法完成风险评级。请在报告中提供设计壁厚(mm)的具体数值。",
            "severity": 0,
            "occurrence": 0,
            "detection": 0,
            "rpn": 0,
            "risk_level": "无法评定",
            "level": 0,
            "standard_ref": "N/A"
        }, ensure_ascii=False)

    try:
        dimensions = {}
        t = defect_type.strip()
        if "裂纹" in t:
            dimensions["length"] = length_mm
            dimensions["depth"] = depth_mm
        elif "气孔" in t or "针孔" in t:
            dimensions["diameter"] = diameter_mm if diameter_mm > 0 else length_mm
            dimensions["density"] = density
        else:
            dimensions["length"] = length_mm
            dimensions["depth"] = depth_mm
            if diameter_mm > 0:
                dimensions["diameter"] = diameter_mm

        result = calc_fmea(defect_type, dimensions, wall_thickness)
        # 确保结果中包含 level 字段（如果 calc_fmea 未返回，可以补上）
        if "level" not in result:
            # 根据 risk_level 简单映射，实际应由 calc_fmea 返回
            mapping = {"高风险": 3, "中风险": 2, "低风险": 1}
            result["level"] = mapping.get(result.get("risk_level"), 0)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"风险评估失败: {str(e)}"}, ensure_ascii=False)


# ===================== 法规检索工具（混合模式） =====================
def _init_regulation_retriever():
    """
    懒加载法规向量库，避免未安装依赖时影响主流程。
    首次调用时初始化，之后复用。
    """
    if hasattr(_init_regulation_retriever, "vectorstore"):
        return _init_regulation_retriever.vectorstore

    try:
        from langchain_community.document_loaders import TextLoader, DirectoryLoader
        from langchain.text_splitter import RecursiveCharacterTextSplitter
        from langchain_community.embeddings import HuggingFaceEmbeddings
        from langchain_community.vectorstores import Chroma
    except ImportError:
        raise ImportError(
            "法规检索功能需要安装 langchain_community、chromadb 等依赖。"
            "请运行: pip install langchain-community chromadb sentence-transformers"
        )

    reg_dir = os.path.join(os.path.dirname(__file__), "regulations")
    if not os.path.isdir(reg_dir):
        raise FileNotFoundError(
            f"法规目录不存在: {reg_dir}，请创建并放入 .txt 法规文件"
        )

    loader = DirectoryLoader(
        reg_dir,
        glob="**/*.txt",
        loader_cls=TextLoader,
        loader_kwargs={"autodetect_encoding": True}
    )
    docs = loader.load()
    if not docs:
        raise FileNotFoundError(f"在 {reg_dir} 中未找到任何法规文本文件")

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=400,
        chunk_overlap=50,
        separators=["\n\n", "\n", "。", ".", " "]
    )
    chunks = text_splitter.split_documents(docs)

    embeddings = HuggingFaceEmbeddings(
        model_name="shibing624/text2vec-base-chinese"
    )

    persist_dir = os.path.join(os.path.dirname(__file__), "chroma_regulation_db")
    vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=persist_dir
    )
    vectorstore.persist()

    _init_regulation_retriever.vectorstore = vectorstore
    print(f"✅ 法规向量库加载完成，共 {len(chunks)} 条片段")
    return vectorstore


def _parse_query(query: str) -> tuple:
    """
    尝试从 query 中提取缺陷类型和风险等级。
    例如："表面裂纹 等级3 处理措施" -> ("表面裂纹", 3)
    如果无法解析，返回 (None, None)
    """
    # 匹配 "xxx 等级N" 模式，N 为数字
    pattern = r'(.+?)\s*等级(\d+)'
    match = re.search(pattern, query)
    if match:
        defect_type = match.group(1).strip()
        level = int(match.group(2))
        return defect_type, level
    return None, None


def _search_regulation_structured(defect_type: str, level: int) -> Optional[dict]:
    """在结构化 JSON 库中精确查找。返回 None 表示未找到。"""
    reg_db = _load_regulation_db()
    if not reg_db:
        return None
    # 缺陷类型名可能有细微差别，先直接匹配，再尝试模糊
    type_dict = reg_db.get(defect_type)
    if not type_dict:
        # 尝试部分匹配（例如用户输入“裂纹”，库中是“表面裂纹”）
        for key in reg_db:
            if defect_type in key or key in defect_type:
                type_dict = reg_db[key]
                break
    if not type_dict:
        return None
    # 获取对应等级
    info = type_dict.get(str(level))
    if not info:
        # 若没有精确等级，向上查找最近的高等级（安全侧原则）
        for lvl in range(level, 5):
            info = type_dict.get(str(lvl))
            if info:
                break
    return info


@tool("search_regulation_tool")
def search_regulation_tool(query: str) -> str:
    """
    从特种设备法规库中检索最相关的处理要求。
    输入格式建议："缺陷类型 等级N 处理措施"，例如："表面裂纹 等级3"。
    优先从结构化法规库（regulations.json）精确查找，未命中则使用向量语义搜索。
    返回 JSON 对象，包含 law_references, mandatory_measures, inspection_advice 字段。
    若等级为0，直接返回无法评定的提示。
    """
    # 1. 尝试解析查询字符串
    defect_type, level = _parse_query(query)

    if defect_type is not None:
        # 2. 等级0特殊处理
        if level == 0:
            return json.dumps({
                "error": "风险等级为0，因缺少壁厚等关键参数无法评定，请补充数据后重新评估。",
                "law_references": "N/A",
                "mandatory_measures": "无法给出",
                "inspection_advice": "需要提供设计壁厚或具体测厚数值"
            }, ensure_ascii=False)

        # 3. 优先从结构化库查询
        structured_result = _search_regulation_structured(defect_type, level)
        if structured_result:
            # 确保结果中包含必需的三个字段
            result = {
                "law_references": structured_result.get("law_references", "无"),
                "mandatory_measures": structured_result.get("mandatory_measures", "无"),
                "inspection_advice": structured_result.get("inspection_advice", "无")
            }
            return json.dumps(result, ensure_ascii=False)

    # 4. 如果结构化库未命中，回退到向量搜索（RAG），但尝试用更完整的查询
    try:
        vs = _init_regulation_retriever()
        docs = vs.similarity_search(query, k=3)
        # 将检索到的片段合并为一段建议
        contents = [doc.page_content for doc in docs]
        joined = "；".join(contents)
        return json.dumps({
            "law_references": "向量检索结果（非精确匹配）",
            "mandatory_measures": joined,
            "inspection_advice": "请参阅相关企业标准文件"
        }, ensure_ascii=False)
    except Exception as e:
        # 若向量库加载失败，返回错误友好信息
        return json.dumps(
            {"error": f"法规检索失败: {str(e)}"},
            ensure_ascii=False
        )