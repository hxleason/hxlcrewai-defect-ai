"""
app/core/exceptions.py
————————————————————
FMEA 系统专用异常体系。

所有业务异常均继承自 FMEABaseException，强制携带：
    - error_code  : 机器可读标识（前端据此渲染不同错误提示）
    - status_code : 建议的 HTTP 响应码（方便 API 层统一处理）
    - message     : 人类可读错误说明
    - detail      : 可选的附加上下文（例如失败的原始数据），默认为 message 的值
    - to_dict()   : 一键将异常转为字典，直接写入数据库 result 字段或返回前端
"""

from typing import Any, Dict, Optional


class FMEABaseException(Exception):
    """FMEA 系统业务异常基类，所有自定义异常均应继承此类"""

    def __init__(
        self,
        message: str,
        error_code: str = "UNKNOWN_ERROR",
        status_code: int = 500,
        detail: Optional[Any] = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.detail = detail if detail is not None else message

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典，可直接存入 Task.result 或 API 响应体"""
        return {
            "error_code": self.error_code,
            "message": self.message,
            "detail": self.detail,
            "status_code": self.status_code,
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.error_code}, msg={self.message!r})"


# ===================== 具体异常子类 =====================

class LLMTimeoutError(FMEABaseException):
    """LLM 调用超时（网络或服务端无响应）"""
    def __init__(self, message: str = "大模型响应超时，请稍后重试", detail: Optional[Any] = None):
        super().__init__(message, error_code="LLM_TIMEOUT", status_code=504, detail=detail)


class LLMAPIError(FMEABaseException):
    """LLM 接口返回错误（鉴权失败、额度耗尽、参数错误等）"""
    def __init__(self, message: str = "大模型接口调用失败", detail: Optional[Any] = None):
        super().__init__(message, error_code="LLM_API_ERROR", status_code=502, detail=detail)


class ParsingError(FMEABaseException):
    """AI 输出解析失败（JSON 格式错误、字段缺失等）"""
    def __init__(self, message: str = "AI 输出格式异常，无法解析为 JSON", detail: Optional[Any] = None):
        super().__init__(message, error_code="PARSING_ERROR", status_code=422, detail=detail)


class ValidationError(FMEABaseException):
    """输入参数校验失败"""
    def __init__(self, message: str = "输入数据不合法", detail: Optional[Any] = None):
        super().__init__(message, error_code="VALIDATION_ERROR", status_code=400, detail=detail)


class RegulationLookupError(FMEABaseException):
    """法规检索失败（向量库未就绪、检索超时等）"""
    def __init__(self, message: str = "法规数据库未就绪", detail: Optional[Any] = None):
        super().__init__(message, error_code="REGULATION_LOOKUP_ERROR", status_code=503, detail=detail)


class TaskNotFoundError(FMEABaseException):
    """任务记录不存在"""
    def __init__(self, task_id: int, detail: Optional[Any] = None):
        message = f"任务 {task_id} 不存在"
        super().__init__(message, error_code="TASK_NOT_FOUND", status_code=404, detail=detail)


class CrewExecutionError(FMEABaseException):
    """多智能体协同执行过程中发生业务逻辑错误"""
    def __init__(self, message: str = "多智能体协同执行失败", detail: Optional[Any] = None):
        super().__init__(message, error_code="CREW_EXECUTION_ERROR", status_code=500, detail=detail)


class DatabaseOperationError(FMEABaseException):
    """数据库读写操作异常"""
    def __init__(self, message: str = "数据库操作失败", detail: Optional[Any] = None):
        super().__init__(message, error_code="DATABASE_ERROR", status_code=500, detail=detail)


class ProcessingTimeoutError(FMEABaseException):
    """某个处理步骤超时（非全局任务超时）"""
    def __init__(self, message: str = "处理步骤超时", detail: Optional[Any] = None):
        super().__init__(message, error_code="PROCESSING_TIMEOUT", status_code=408, detail=detail)