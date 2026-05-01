from typing import Any


class FriendlyException(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        status_code: int = 400,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data
