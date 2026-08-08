"""
app/routers/fmea.py
———————————————
FMEA 评估专用路由（直接调用稳健解析 Crew）
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.crews import run_fmea_evaluation, run_full_fmea_evaluation

router = APIRouter(
    prefix="/fmea",
    tags=["FMEA 评估"],
    responses={404: {"description": "Not found"}},
)


class TextInput(BaseModel):
    text: str = Field(..., description="待评估的特种设备检测报告全文")
    project_id: int | None = Field(None, description="可选：关联的项目 ID，用于后续落库")


@router.post("/evaluate", summary="FMEA 评估（不含法规审核）")
async def evaluate_fmea(input_data: TextInput):
    """
    接收报告文本，返回 FMEA 评估结果：
    - 自动提取缺陷
    - 调用风险评定工具和诊断工具
    - 内置 JSON 智能修复，杜绝解析失败
    """
    try:
        result = run_fmea_evaluation(input_data.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"评估失败: {str(e)}")

    if "parse_error" in result:
        raise HTTPException(status_code=422, detail=f"LLM 输出解析失败: {result['parse_error']}")

    return result


@router.post("/evaluate/full", summary="完整 FMEA 评估（含法规审核）")
async def evaluate_fmea_full(input_data: TextInput):
    """
    在 FMEA 基础上自动查询法规条文，为每条缺陷补充：
    - law_references
    - mandatory_measures
    - inspection_advice
    """
    try:
        result = run_full_fmea_evaluation(input_data.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"完整评估失败: {str(e)}")

    if "parse_error" in result:
        raise HTTPException(status_code=422, detail=f"LLM 输出解析失败: {result['parse_error']}")

    return result