"""跨层错误；HTTP 映射属于 Controller，底层原因仅供日志使用。"""

from __future__ import annotations


class ServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


class ServiceNotBoundError(RuntimeError):
    """仅表示装配缺失；不代表三方调用失败，不可用于自动重发请求。"""


class DataAccessError(RuntimeError):
    def __init__(self, kind: str, message: str = "数据访问失败") -> None:
        super().__init__(message)
        self.kind = kind


class ClientError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "upstream", provider: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.kind, self.provider, self.code = kind, provider, code


class TransactionError(DataAccessError):
    def __init__(self) -> None:
        super().__init__("rollback_only", "事务中的操作失败，已回滚")
