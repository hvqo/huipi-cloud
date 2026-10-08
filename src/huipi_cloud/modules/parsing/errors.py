"""Safe, typed parser failures used for retry classification."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureSummary:
    code: str
    message: str
    retryable: bool


class ParsingExecutionError(Exception):
    """Base error whose public summary never contains a raw exception message."""

    summary = FailureSummary("parsing_error", "解析任务执行失败", True)


class RetryableParsingError(ParsingExecutionError):
    """A temporary failure that may succeed on a later attempt."""

    def __init__(self, code: str = "temporary_error") -> None:
        summaries = {
            "object_store_unavailable": FailureSummary(
                "object_store_unavailable", "原始文件暂时无法读取", True
            ),
            "network_timeout": FailureSummary("network_timeout", "解析服务暂时无响应", True),
            "resource_unavailable": FailureSummary(
                "resource_unavailable", "解析资源暂时不足", True
            ),
            "execution_timeout": FailureSummary(
                "execution_timeout", "解析超过单次执行时限", True
            ),
            "parser_unavailable": FailureSummary(
                "parser_unavailable", "本地解析资源暂时不可用", True
            ),
            "temporary_error": FailureSummary("temporary_error", "解析暂时失败，请稍后重试", True),
        }
        self.summary = summaries.get(code, summaries["temporary_error"])
        super().__init__(self.summary.code)


class PermanentParsingError(ParsingExecutionError):
    """A deterministic input or format failure that must not be retried."""

    def __init__(self, code: str = "invalid_input") -> None:
        summaries = {
            "unsupported_file_type": FailureSummary(
                "unsupported_file_type", "不支持的文件格式", False
            ),
            "corrupt_input": FailureSummary("corrupt_input", "文件内容无法解析", False),
            "invalid_input": FailureSummary("invalid_input", "文件内容无效", False),
            "invalid_result": FailureSummary("invalid_result", "解析结果不完整或格式无效", False),
            "missing_source": FailureSummary("missing_source", "原始文件不存在", False),
            "page_limit_exceeded": FailureSummary(
                "page_limit_exceeded", "PDF页数超过处理上限", False
            ),
        }
        self.summary = summaries.get(code, summaries["invalid_input"])
        super().__init__(self.summary.code)


def classify_failure(error: BaseException) -> FailureSummary:
    """Map executor errors to bounded, non-sensitive task status text."""
    if isinstance(error, ParsingExecutionError):
        return error.summary
    return FailureSummary("unexpected_error", "解析暂时失败，请稍后重试", True)


class ParsingResultNotReadyError(Exception):
    """Raised when parsed content is requested before a successful result exists."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


class WorkerFatalParsingError(RuntimeError):
    """Raised when a parser child cannot be safely reaped and this worker must stop."""
