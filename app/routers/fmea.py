"""
app/routers/fmea.py
FMEA 评估专用路由（v1.2 · 同步路径审核门接入）

核心特性：
    - 所有耗时的 Crew 任务通过 asyncio.to_thread() 放入线程池执行，避免阻塞事件循环。
    - 每个端点独立设置超时时间，防止永久等待。
    - 统一将 FMEABaseException 转换为结构化 HTTP 错误，便于前端识别。
    - 保持 async 函数签名，兼容异步中间件及未来扩展。
    - 使用新版 run_full_fmea 函数，返回完整评估结果（含法规审核与审核标记）。

v1.1 修复说明（保留）：
    - 修复 evaluate_fmea / evaluate_fmea_full 中错误调用 .model_dump() 导致的
      AttributeError（run_full_fmea 返回 dict，而非 Pydantic 对象）。
    - 更新模块 docstring 与端点注释，使其与实际调用行为一致。
    - 保留 /evaluate 与 /evaluate/full 两个端点以维持 API 向后兼容，
      两者当前调用统一入口 run_full_fmea，仅超时时间不同。

v1.2 修复说明（本次）：
    - 同步路径接入统一审核闸门（评估报告 6.1）：
      结果新增 review_pending / review_required_ids 字段；
      当 review_pending=True 时记录告警日志，前端必须引导人工审核，
      未审核前不得将结果作为最终结论。
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
        func 的原始返回值（原样透传，调用方需自行判断具体类型）

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
        logger.error(
            "任务超时：%s 执行超过 %ss",
            getattr(func, "__name__", "unknown"),
            timeout,
        )
        raise HTTPException(
            status_code=504,
            detail={
                "error_code": "TASK_TIMEOUT",
                "message": f"任务执行超时（>{timeout}秒），请简化报告内容或稍后重试。",
            },
        )
    except FMEABaseException as e:
        # 自定义业务异常，直接映射为 HTTP 异常
        logger.warning("业务异常 [%s]: %s", e.error_code, e)
        raise HTTPException(
            status_code=e.status_code,
            detail={
                "error_code": e.error_code,
                "message": e.message,
                "detail": e.detail,
            },
        )
    except Exception as e:
        # 未预期的系统错误
        logger.exception("系统内部错误: %s", e)
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "INTERNAL_ERROR",
                "message": f"系统内部错误: {e}",
            },
        )


def _log_review_pending(result: Dict[str, Any]) -> None:
    """
    v1.2 新增：同步路径审核门告警。
    当结果存在待审核缺陷时记录告警日志，前端收到 review_pending=True
    后必须引导人工审核，未审核前不得作为最终结论。
    """
    if result.get("review_pending"):
        logger.warning(
            "同步评估结果包含待审核缺陷 ids=%s，未审核前不得作为最终结论",
            result.get("review_required_ids"),
        )


@router.post(
    "/evaluate",
    summary="FMEA 评估（含缺陷提取、规则评级与法规审核）",
    response_description="完整的 FMEA 评估结果字典，包含 defects / report_summary / "
                         "failed_count / failed_defect_ids / review_pending / review_required_ids",
)
async def evaluate_fmea(input_data: TextInput) -> Dict[str, Any]:
    """
    接收报告文本，返回 FMEA 完整评估结果（同步路径）。

    内部流程（由 run_full_fmea 完成）：
    1. LLM 缺陷提取（仅一次 LLM 调用）
    2. 规则引擎评级（evaluate_one_defect，确定性计算）
    3. 规则引擎审计（audit_one_defect，含法规检索与 PWHT 建议）
    4. 统一审核闸门判定（与异步路径一致）

    返回结构（dict）：
    {
        "defects": [...],              // 审计后的完整缺陷列表
        "report_summary": "...",       // 汇总信息
        "failed_count": 0,             // 处理失败的缺陷数
        "failed_defect_ids": [],       // 失败缺陷的 ID 列表
        "review_pending": false,       // 是否存在待人工审核缺陷
        "review_required_ids": []      // 待人工审核缺陷的 ID 列表
    }

    可能返回的错误码：
    - LLM_TIMEOUT (504)
    - LLM_API_ERROR (502)
    - PARSING_ERROR (422)
    - TASK_TIMEOUT (504)
    - INTERNAL_ERROR (500)
    """
    result = await run_in_thread(
        run_full_fmea,  # 新版统一入口（返回 dict）
        input_data.text,
        timeout=300,  # 根据实际 LLM 耗时调整，给予 5 分钟
    )
    # 同步路径同样受审核闸门约束（评估报告 6.1）
    _log_review_pending(result)
    # run_full_fmea 返回纯 dict，直接返回即可（无需 .model_dump()）
    return result


@router.post(
    "/evaluate/full",
    summary="完整 FMEA 评估（同步路径，超时更长）",
    response_description="与 /evaluate 返回结构完全一致（含 review_pending / review_required_ids）",
)
async def evaluate_fmea_full(input_data: TextInput) -> Dict[str, Any]:
    """
    与 /evaluate 行为一致，仅超时时间更长（420s），适合较长报告或复杂缺陷场景。

    注：两个端点当前调用同一入口 run_full_fmea。
    保留本端点是为了 API 向后兼容，并为未来分离
    「轻量评估」与「完整评估」路径预留接口空间。

    可能额外出现的错误码：
    - REGULATION_LOOKUP_ERROR (503)
    - 同 /evaluate 的其他错误码
    """
    result = await run_in_thread(
        run_full_fmea,  # 新版统一入口（返回 dict）
        input_data.text,
        timeout=420,  # 预留更充裕的 LLM 与法规查询时间（7 分钟）
    )
    # 同步路径同样受审核闸门约束（评估报告 6.1）
    _log_review_pending(result)
    # run_full_fmea 返回纯 dict，直接返回即可
    return result