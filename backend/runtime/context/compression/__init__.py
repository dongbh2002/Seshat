"""上下文压缩包，对外提供压缩策略基类、历史归档结构、按轮分组工具及分级压缩实现。"""

from backend.runtime.context.compression.base import (
    CompressionResult,
    CompressionStats,
    CompressionStrategy,
    Context,
    HistoryArchive,
    group_completed_turns,
)
from backend.runtime.context.compression.tiered import TieredCompressionStrategy

__all__ = [
    "CompressionResult",
    "CompressionStats",
    "CompressionStrategy",
    "Context",
    "HistoryArchive",
    "TieredCompressionStrategy",
    "group_completed_turns",
]
