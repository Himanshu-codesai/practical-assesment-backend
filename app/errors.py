"""Application errors. Each maps to one HTTP status in app.main."""


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class BadRequest(AppError):
    status_code = 422
    code = "bad_request"


class PipelineError(AppError):
    """A background step failed. `retryable` tells the worker whether trying again can help."""

    code = "pipeline_error"

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMError(PipelineError):
    status_code = 502
    code = "llm_error"
