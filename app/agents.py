"""
app/agents.py - LLM 实例管理（环境变量驱动版）
无需任何额外的 config 文件，所有参数均可通过环境变量覆盖。
"""

import os
import logging
from crewai import LLM

logger = logging.getLogger("defect_fmea.agents")

llm = None   # 不在这里实例化，延迟创建


def get_llm() -> LLM:
    """
    延迟创建 LLM 实例，避免导入时因环境/网络问题崩溃。
    优先从环境变量读取配置，未设置时使用安全默认值。
    """
    global llm
    if llm is None:
        logger.info("正在创建 LLM 实例...")
        try:
            llm = LLM(
                model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
                base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
                api_key=os.getenv("LLM_API_KEY", ""),          # 必须提供有效 key！
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
            )
            logger.info("LLM 实例化成功")
        except Exception as e:
            logger.error(f"创建 LLM 失败: {e}")
            raise
    return llm