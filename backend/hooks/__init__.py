"""Hook 基础设施包，对外提供同步生命周期 Hook 引擎及其数据结构。"""

from backend.hooks.engine import (
    HookContext,
    HookEngine,
    HookEvent,
    HookExecutionError,
    HookHandler,
    HookRejectedError,
    HookScope,
)

__all__ = [
    "HookContext",
    "HookEngine",
    "HookEvent",
    "HookExecutionError",
    "HookHandler",
    "HookRejectedError",
    "HookScope",
]
