class AppError(Exception):
    """面向调用方的业务错误：code 给前端判断，message 给人看，status 是 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
