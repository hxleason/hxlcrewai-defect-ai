"""
app/routers/fmea.py
FMEA 评估专用路由（异步 + 线程池 + 超时保护终极版）

核心特性：
- 所有耗时的 Crew 任务均通过 asyncio.to_thread() 放入线程池执行。
- 每个端点独立设置超时时间，避免永久等待。
- 统一转换 FMEABaseException 为结构化 HTTP 错误。
- 保持 async 函数签名，兼容异步中间件及未来扩展。
"""
import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.crews import run_fmea_evaluation, run_full_fmea_evaluation
from app.core.exceptions import FMEABaseException

logger = logging.getLogger("defect_fmea.api")

router = APIRouter(
    prefix="/fmea",
    tags=["FMEA 评估"],
    responses={404: {"description": "Not found"}},
)

class TextInput(BaseModel):
    text: str = Field(
        ..., 
        min_length=10, 
        description="待评估的特种设备检测报告全文"
    )
    project_id: int | None = Field(
        None, 
        description="可选：关联的项目 ID，用于后续落库"
    )


async def run_in_thread(func, *args, timeout: float = 120.0):
    """
    将同步阻塞函数放入线程池执行，并提供超时和异常转换。

    参数:
        func:   要执行的同步函数
        *args:  传递给 func 的位置参数
        timeout: 超时时间（秒），默认 120 秒

    返回:
        函数 func 的原始返回值

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
        logger.error(f"任务超时：{func.__name__} 执行超过 {timeout}s")
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


@router.post("/evaluate", summary="FMEA 评估（不含法规审核）")
async def evaluate_fmea(input_data: TextInput):
    """
    接收报告文本，返回 FMEA 评估结果：
    - 自动提取缺陷
    - 调用风险评定工具和诊断工具
    - 内置 JSON 修复机制，杜绝解析失败

    可能返回的错误码：
    - LLM_TIMEOUT (504)
    - LLM_API_ERROR (502)
    - PARSING_ERROR (422)
    - INTERNAL_ERROR (500)
    """
    result = await run_in_thread(
        run_fmea_evaluation,
        input_data.text,
        timeout=120  # 普通评估给予 2 分钟
    )
    return result


@router.post("/evaluate/full", summary="完整 FMEA 评估（含法规审核）")
async def evaluate_fmea_full(input_data: TextInput):
    """
    在 FMEA 基础上自动查询法规条文，为每条缺陷补充：
    - 法律条文引用
    - 强制措施
    - 检查建议

    可能额外出现的错误码：
    - REGULATION_LOOKUP_ERROR (503)
    - 同 /evaluate 的其他错误码
    """
    result = await run_in_thread(
        run_full_fmea_evaluation,
        input_data.text,
        timeout=180  # 含法规查询，给予 3 分钟
    )
    return result