"""通过 Runtime 生命周期 Hook 统一记录模型、工具和运行时日志。"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, ClassVar

from backend.logging.formatter import log_event
from backend.runtime.hook_engine import HookContext, HookEngine, HookEvent


class LoggingHook:
    """将所有 HookContext 转换为字段一致的结构化日志事件。"""

    priority: ClassVar[int] = -1000  # 确保日志观察到其他 Hook 修改后的最终数据。
    started_at_key: ClassVar[str] = "_logging_started_at"  # 生命周期开始时间键。
    traceback_marker: ClassVar[str] = (  # 防止同一异常在多个边界重复打印堆栈。
        "_seshat_traceback_logged"
    )
    before_events: ClassVar[frozenset[HookEvent]] = frozenset(  # 输入事件集合。
        {
            HookEvent.BEFORE_RUNTIME,
            HookEvent.BEFORE_MODEL_REQUEST,
            HookEvent.BEFORE_TOOL_EXECUTE,
        }
    )
    after_events: ClassVar[frozenset[HookEvent]] = frozenset(  # 输出事件集合。
        {
            HookEvent.AFTER_RUNTIME,
            HookEvent.AFTER_MODEL_RESPONSE,
            HookEvent.AFTER_TOOL_EXECUTE,
        }
    )
    error_events: ClassVar[frozenset[HookEvent]] = frozenset(  # 失败事件集合。
        {
            HookEvent.RUNTIME_ERROR,
            HookEvent.MODEL_ERROR,
            HookEvent.TOOL_ERROR,
            HookEvent.TOOL_CALL_ERROR,
        }
    )

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """初始化生命周期日志处理器。

        Args:
            logger: 可选的目标日志记录器；省略时使用项目生命周期记录器。

        Returns:
            None。
        """
        self.logger = logger or logging.getLogger(  # 生命周期事件输出入口。
            "backend.lifecycle"
        )

    def register(self, hook_engine: HookEngine) -> None:
        """以最低优先级将当前日志处理器注册到全部生命周期事件。

        Args:
            hook_engine: Runtime、AgentLoop 和 ToolEngine 共享的 HookEngine。

        Returns:
            None。
        """
        for event in HookEvent:
            hook_engine.register(
                event,
                self,
                priority=self.priority,
                critical=False,
            )

    def __call__(self, context: HookContext) -> None:
        """把一次生命周期事件转换为结构化日志。

        Args:
            context: 当前事件的范围、输入、结果、错误和共享元数据。

        Returns:
            None。
        """
        data = self._common_fields(context)
        data.update(self._event_fields(context))
        error_for_traceback = (
            self._claim_traceback(context.error)
            if context.event in self.error_events
            else None
        )
        level = logging.ERROR if context.event in self.error_events else logging.INFO
        log_event(
            self.logger,
            context.event.value,
            data,
            level=level,
            error=error_for_traceback,
        )
        if context.event in self.before_events:
            context.metadata[self.started_at_key] = perf_counter()

    def _common_fields(self, context: HookContext) -> dict[str, Any]:
        """生成每个生命周期事件都具有的关联字段。

        Args:
            context: 当前生命周期上下文。

        Returns:
            包含运行范围、步骤和工具调用标识的字段字典。
        """
        return {
            "run_id": context.scope.run_id,
            "tenant_id": context.scope.tenant_id,
            "user_id": context.scope.user_id,
            "session_name": context.scope.session_name,
            "step": context.metadata.get("step"),
            "tool_call_id": context.metadata.get("tool_call_id"),
        }

    def _event_fields(self, context: HookContext) -> dict[str, Any]:
        """根据事件阶段生成输入、输出、错误和耗时字段。

        Args:
            context: 当前生命周期上下文。

        Returns:
            当前事件特有的结构化字段。
        """
        fields: dict[str, Any] = {}
        if context.event in self.before_events:
            fields["input"] = context.payload
        elif context.event in self.after_events:
            fields["output"] = context.result
            duration_ms = self._duration_ms(context)
            if duration_ms is not None:
                fields["duration_ms"] = duration_ms
        elif context.event in self.error_events:
            fields["input"] = context.payload
            if context.error is not None:
                fields["error_type"] = type(context.error).__name__
                fields["error"] = str(context.error)
            duration_ms = self._duration_ms(context)
            if duration_ms is not None:
                fields["duration_ms"] = duration_ms
        return fields

    def _duration_ms(self, context: HookContext) -> float | None:
        """计算当前生命周期从 before 事件开始经过的毫秒数。

        Args:
            context: 保存开始时间的生命周期上下文。

        Returns:
            三位小数的毫秒数；缺少有效开始时间时返回 None。
        """
        started_at = context.metadata.get(self.started_at_key)
        if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
            return None
        return round((perf_counter() - started_at) * 1000, 3)

    def _claim_traceback(
        self,
        error: BaseException | None,
    ) -> BaseException | None:
        """仅为第一次遇到的异常对象申请调用栈记录权。

        Args:
            error: 当前生命周期事件携带的异常。

        Returns:
            尚未记录过调用栈的异常；否则返回 None。
        """
        if error is None or getattr(error, self.traceback_marker, False):
            return None
        try:
            setattr(error, self.traceback_marker, True)
        except (AttributeError, TypeError):
            # TODO: 若后续出现禁止扩展属性的异常类型，增加运行级异常 ID 集合兜底。
            return error
        return error
