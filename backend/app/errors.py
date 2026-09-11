from __future__ import annotations


class RecognitionError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ConfigurationError(RecognitionError):
    def __init__(self) -> None:
        super().__init__(
            "service_not_configured",
            "请先配置识图接口和 AI 回复接口的 API Key。",
            status_code=503,
        )


class ImageValidationError(RecognitionError):
    def __init__(self, code: str, message: str, *, status_code: int = 415) -> None:
        super().__init__(code, message, status_code=status_code)


class ProviderError(RecognitionError):
    def __init__(self, code: str, message: str = "图片识别服务暂时不可用，请稍后重试。") -> None:
        super().__init__(code, message, status_code=502)
