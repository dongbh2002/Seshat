"""提供单行 JSON 日志格式化和结构化事件写入函数。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar

_SENSITIVE_KEYS = frozenset(  # 写入日志前必须隐藏的认证字段名称。
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "x_api_key",
    }
)


class JsonFormatter(logging.Formatter):
    """将一个 LogRecord 格式化为包含毫秒和时区的单行 JSON。"""

    reserved_fields: ClassVar[frozenset[str]] = frozenset(  # 标准日志保留字段。
        {"timestamp", "level", "logger", "event", "exception"}
    )

    def format(self, record: logging.LogRecord) -> str:
        """把日志记录及其结构化扩展字段序列化为单行 JSON。

        Args:
            record: Python logging 生成的原始日志记录。

        Returns:
            保留中文、转义换行并包含标准字段的 JSON 字符串。
        """
        event = getattr(record, "event", "log")
        raw_event_data = getattr(record, "event_data", {})
        event_data = (
            sanitize_log_data(raw_event_data)
            if isinstance(raw_event_data, Mapping)
            else {"data": sanitize_log_data(raw_event_data)}
        )
        payload = {
            key: value
            for key, value in event_data.items()
            if key not in self.reserved_fields
        }
        payload.update(
            {
                "timestamp": datetime.fromtimestamp(record.created)
                .astimezone()
                .isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "event": str(event),
            }
        )
        if event == "log":
            payload["message"] = record.getMessage()
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )


def log_event(
    logger: logging.Logger,
    event: str,
    data: Mapping[str, Any],
    *,
    level: int = logging.INFO,
    error: BaseException | None = None,
) -> None:
    """通过标准 logging 写入一个结构化事件。

    Args:
        logger: 接收事件的日志记录器。
        event: 稳定、可检索的事件名称。
        data: 需要写入 JSON 顶层的事件字段。
        level: Python logging 日志级别。
        error: 可选异常；提供时记录该异常当前保存的调用栈。

    Returns:
        None。
    """
    exception_info = (
        (type(error), error, error.__traceback__) if error is not None else None
    )
    logger.log(
        level,
        event,
        extra={"event": event, "event_data": dict(data)},
        exc_info=exception_info,
    )


def sanitize_log_data(value: Any) -> Any:
    """递归复制日志数据并隐藏常见认证字段。

    Args:
        value: 需要检查的任意日志数据。

    Returns:
        可安全写入日志的基础类型、字典或列表。
    """
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            string_key = str(key)
            normalized_key = string_key.lower().replace("-", "_")
            sanitized[string_key] = (
                "***REDACTED***"
                if normalized_key in _SENSITIVE_KEYS
                else sanitize_log_data(item)
            )
        return sanitized
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [sanitize_log_data(item) for item in value]
    return value
