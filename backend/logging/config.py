"""配置 Seshat 本地 JSON 文件日志的级别、路径和轮转策略。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from backend.logging.formatter import JsonFormatter

_LOGGER_NAME = "backend"  # 项目模块共享的顶层日志记录器名称。
_HANDLER_NAME = "seshat_file"  # 用于避免重复添加文件处理器的名称。
_LOG_LEVELS = {  # Python 支持版本内允许配置的标准日志级别。
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def configure_logging(
    settings: Mapping[str, Any],
    *,
    base_directory: Path,
) -> Path:
    """根据项目配置创建带轮转能力的 UTF-8 文件日志。

    Args:
        settings: 包含日志级别、路径、大小上限和备份数量的配置对象。
        base_directory: 相对日志路径使用的项目根目录。

    Returns:
        实际使用的绝对日志文件路径。

    Raises:
        TypeError: 配置字段类型无效。
        ValueError: 日志级别、大小上限或备份数量无效。
    """
    if not isinstance(settings, Mapping):
        raise TypeError("配置中的 logging 必须是对象")

    level_name = settings.get("level", "INFO")
    if not isinstance(level_name, str):
        raise TypeError("logging.level 必须是字符串")
    normalized_level = level_name.upper()
    level = _LOG_LEVELS.get(normalized_level)
    if level is None:
        raise ValueError(f"不支持的日志级别: {level_name}")

    path_value = settings.get("path", "logs/seshat.log")
    if not isinstance(path_value, str) or not path_value.strip():
        raise TypeError("logging.path 必须是非空字符串")
    configured_path = Path(path_value)
    log_path = (
        configured_path
        if configured_path.is_absolute()
        else base_directory / configured_path
    ).resolve()

    max_bytes = settings.get("max_bytes", 10 * 1024 * 1024)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        raise TypeError("logging.max_bytes 必须是整数")
    if max_bytes <= 0:
        raise ValueError("logging.max_bytes 必须大于 0")

    backup_count = settings.get("backup_count", 5)
    if not isinstance(backup_count, int) or isinstance(backup_count, bool):
        raise TypeError("logging.backup_count 必须是整数")
    if backup_count < 0:
        raise ValueError("logging.backup_count 不能小于 0")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.set_name(_HANDLER_NAME)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())

    project_logger = logging.getLogger(_LOGGER_NAME)
    project_logger.setLevel(level)
    project_logger.propagate = False
    for existing_handler in tuple(project_logger.handlers):
        if existing_handler.get_name() == _HANDLER_NAME:
            project_logger.removeHandler(existing_handler)
            existing_handler.close()
    project_logger.addHandler(handler)
    return log_path
