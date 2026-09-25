"""配置 Seshat 本地 JSON 文件日志的级别、路径和轮转策略。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.config import LoggingSettings
from backend.logging.formatter import JsonFormatter

_LOGGER_NAME = "backend"  # 项目模块共享的顶层日志记录器名称。
_HANDLER_NAME = "seshat_file"  # 用于避免重复添加文件处理器的名称。


def configure_logging(
    settings: LoggingSettings,
    *,
    base_directory: Path,
) -> Path:
    """根据项目配置创建带轮转能力的 UTF-8 文件日志。

    Args:
        settings: 已完成校验的类型化日志配置。
        base_directory: 相对日志路径使用的项目根目录。

    Returns:
        实际使用的绝对日志文件路径。

    """
    level = getattr(logging, settings.level)
    configured_path = Path(settings.path)
    log_path = (
        configured_path
        if configured_path.is_absolute()
        else base_directory / configured_path
    ).resolve()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
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
