import logging
from crewai import LLM
from app.config import LLM_MODEL, LLM_BASE_URL, LLM_API_KEY, LLM_TEMPERATURE, LLM_MAX_TOKENS

logger = logging.getLogger("defect_fmea.agents")

llm = None   # 不在这里实例化

def get_llm() -> LLM:
    """延迟创建 LLM 实例，避免导入时因环境/网络问题崩溃"""
    global llm
    if llm is None:
        logger.info("正在创建 LLM 实例...")
        try:
            llm = LLM(
                model=LLM_MODEL,
                base_url=LLM_BASE_URL,
                api_key=LLM_API_KEY,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
            )
            logger.info("LLM 实例化成功")
        except Exception as e:
            logger.error(f"创建 LLM 失败: {e}")
            raise
    return llm