import json
import inspect
from typing import Any, Dict, List, Optional, Callable
from pydantic import BaseModel, Field, create_model
from crewai.tools import BaseTool as CrewBaseTool

from app.core.utils import fmea_calculator, diagnosis_reasons
from app.core.regulation import search_regulation

class DynamicTool(CrewBaseTool):
    func: Optional[Callable] = Field(default=None, exclude=True)

    def _run(self, **kwargs) -> str:
        if self.func is None:
            return json.dumps({"error": "工具函数未设置"}, ensure_ascii=False)
        try:
            result = self.func(**kwargs)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

def create_crewai_tool(func: Callable, name: str, description: str) -> DynamicTool:
    sig = inspect.signature(func)
    fields = {}
    for pname, p in sig.parameters.items():
        if pname in ("kwargs", "return"):
            continue
        ann = p.annotation if p.annotation is not inspect.Parameter.empty else Any
        default = p.default if p.default is not inspect.Parameter.empty else ...
        fields[pname] = (ann, Field(default=default))
    args_schema = create_model(f"{name}_args", **fields) if fields else None
    return DynamicTool(
        name=name,
        description=description,
        func=func,
        args_schema=args_schema,
    )

# ---------- 三个全局工具实例 ----------
diagnosis_tool = create_crewai_tool(
    diagnosis_reasons,
    "diagnosis_tool",
    "根据缺陷类型返回可能原因。参数: defect_type (str)。",
)

risk_tool = create_crewai_tool(
    fmea_calculator,
    "risk_assessment_tool",
    "计算 FMEA 风险等级。参数: defect_type (str), length_mm (float 或 None), depth_mm (float 或 None), wall_thickness (float 或 None), quantity (int)。返回 severity, occurrence, detection, rpn, risk_level 等。",
)

regulation_tool = create_crewai_tool(
    search_regulation,
    "search_regulation_tool",
    "检索法规与标准。参数: query (str)。自动结合结构化法规库和标准全文库。",
)
