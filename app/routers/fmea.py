"""
app/routers/fmea.py
FMEA 评估专用路由（终极版）

核心特性：
    - 所有耗时的 Crew 任务通过 asyncio.to_thread() 放入线程池执行，避免阻塞事件循环。
    - 每个端点独立设置超时时间，防止永久等待。
    - 统一将 FMEABaseException 转换为结构化 HTTP 错误，便于前端识别。
    - 保持 async 函数签名，兼容异步中间件及未来扩展。
    - 直接使用新版 run_full_fmea 函数，返回完整评估结果（含 AP 与审核标记）。
"""

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# 导入新版端到端函数（推荐使用，不再调用废弃函数）
from app.crews import run_full_fmea
from app.core.exceptions import FMEABaseException

logger = logging.getLogger("defect_fmea.api")

# 创建路由器
router = APIRouter(
    prefix="/fmea",
    tags=["FMEA 评估"],
    responses={404: {"description": "Not found"}},
)


class TextInput(BaseModel):
    """
    请求体模型：待评估的特种设备检验报告文本。
    """
    text: str = Field(
        ...,
        min_length=10,
        max_length=100_000,
        description="待评估的特种设备检测报告全文（10~100000 字符）",
    )
    project_id: Optional[int] = Field(
        None,
        description="可选：关联的项目 ID，用于后续落库（当前版本暂未使用）",
    )


async def run_in_thread(
    func: Callable,
    *args: Any,
    timeout: float = 120.0,
) -> Any:
    """
    将同步阻塞函数放入线程池执行，并提供超时和异常转换。

    参数:
        func:     要执行的同步函数
        *args:    传递给 func 的位置参数
        timeout:  超时时间（秒），默认 120 秒

    返回:
        func 的原始返回值

    抛出:
        HTTPException(504)：任务超时
        HTTPException(4xx/5xx)：业务异常或系统错误（结构化 JSON）
    """
    try:
        # asyncio.to_thread 将同步调用扔进线程池，不阻塞事件循环
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error(f"任务超时：{getattr(func, '__name__', 'unknown')} 执行超过 {timeout}s")
        raise HTTPException(
            status_code=504,
            detail={
                "error_code": "TASK_TIMEOUT",
                "message": f"任务执行超时（>{timeout}秒），请简化报告内容或稍后重试。",
            },
        )
    except FMEABaseException as e:
        # 自定义业务异常，直接映射为 HTTP 异常
        logger.warning(f"业务异常 [{e.error_code}]: {str(e)}")
        raise HTTPException(
            status_code=e.status_code,
            detail={
                "error_code": e.error_code,
                "message": str(e),
            },
        )
    except Exception as e:
        # 未预期的系统错误
        logger.exception(f"系统内部错误: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "INTERNAL_ERROR",
                "message": f"系统内部错误: {str(e)}",
            },
        )


@router.post(
    "/evaluate",
    summary="FMEA 评估（不含法规审核）",
    response_description="完整的 FMEA 评估结果清单",
)
async def evaluate_fmea(input_data: TextInput) -> Dict[str, Any]:
    """
    接收报告文本，返回 FMEA 评估结果：
    - 自动提取缺陷
    - 调用风险评定工具和诊断工具
    - 内置 JSON 修复机制，杜绝解析失败

    可能返回的错误码：
    - LLM_TIMEOUT (504)
    - LLM_API_ERROR (502)
    - PARSING_ERROR (422)
    - TASK_TIMEOUT (504)
    - INTERNAL_ERROR (500)
    """
    result = await run_in_thread(
        run_full_fmea,  # 新版统一入口
        input_data.text,
        timeout=300,  # 根据实际 LLM 耗时调整，给予 5 分钟
    )
    # run_full_fmea 返回 FMEAAnalysisResult 对象，需要序列化为字典
    return result.model_dump()


@router.post(
    "/evaluate/full",
    summary="完整 FMEA 评估（预留法规审核）",
    response_description="包含 FMEA 评估结果，暂不包含法规审核详情",
)
async def evaluate_fmea_full(input_data: TextInput) -> Dict[str, Any]:
    """
    在 FMEA 基础上预留法规审核功能，当前版本与 /evaluate 行为一致。
    后续可通过扩展 run_full_fmea 或添加额外步骤实现法规条文查询。

    可能额外出现的错误码：
    - REGULATION_LOOKUP_ERROR (503)
    - 同 /evaluate 的其他错误码
    """
    result = await run_in_thread(
        run_full_fmea,  # 新版统一入口
        input_data.text,
        timeout=420,  # 预留法规查询时间，给予 7 分钟
    )
    return result.model_dump()