"""提供 Runtime、模型调用和工具执行共享的同步生命周期 Hook 机制。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar

_LOGGER = logging.getLogger(__name__)


class HookEvent(str, Enum):
    """定义当前 Runtime 支持的生命周期 Hook 事件。"""

    BEFORE_RUNTIME = "before_runtime"  # Runtime 开始处理用户输入前。
    AFTER_RUNTIME = "after_runtime"  # Runtime 成功得到最终回复后。
    RUNTIME_ERROR = "runtime_error"  # Runtime 处理失败时。
    BEFORE_MODEL_REQUEST = "before_model_request"  # 每次模型请求发出前。
    AFTER_MODEL_RESPONSE = "after_model_response"  # 每次模型响应返回后。
    MODEL_ERROR = "model_error"  # 模型请求或响应 Hook 失败时。
    BEFORE_TOOL_EXECUTE = "before_tool_execute"  # 工具参数提交执行前。
    AFTER_TOOL_EXECUTE = "after_tool_execute"  # 工具成功执行后。
    TOOL_ERROR = "tool_error"  # 工具执行或工具 Hook 失败时。


@dataclass(frozen=True)
class HookScope:
    """保存一次 Hook 调用所属的租户、用户、会话和运行标识。"""

    tenant_id: str | None = None  # 当前租户标识。
    user_id: str | None = None  # 当前用户标识。
    session_name: str | None = None  # 当前会话名称。
    run_id: str | None = None  # 当前 Runtime.run 调用的唯一标识。

    def with_run_id(self, run_id: str) -> HookScope:
        """复制当前范围并设置一次运行的唯一标识。

        Args:
            run_id: 当前 Runtime.run 调用的唯一标识。

        Returns:
            带有指定 run_id 的新 HookScope。
        """
        return replace(self, run_id=run_id)


@dataclass
class HookContext:
    """在 Hook 之间传递生命周期数据、结果、错误和扩展元数据。"""

    event: HookEvent  # 当前触发的生命周期事件。
    scope: HookScope = field(default_factory=HookScope)  # 当前运行范围。
    payload: dict[str, Any] = field(default_factory=dict)  # Hook 可检查或修改的输入。
    metadata: dict[str, Any] = field(default_factory=dict)  # Hook 间共享的扩展数据。
    result: Any = None  # after 事件对应的执行结果。
    error: BaseException | None = None  # error 事件对应的异常。


class HookRejectedError(RuntimeError):
    """表示 Hook 根据权限或业务规则主动拒绝继续执行。"""


class HookExecutionError(RuntimeError):
    """表示 critical Hook 自身执行失败。"""


HookHandler = Callable[[HookContext], Any]


@dataclass(frozen=True)
class _RegisteredHook:
    """保存一个 Hook 处理函数及其执行策略。"""

    handler: HookHandler  # Hook 事件触发时调用的同步处理函数。
    priority: int  # 执行优先级，数值越大越先执行。
    critical: bool  # 失败时是否中止当前核心流程。
    order: int  # 同优先级 Hook 的稳定注册顺序。


class HookEngine:
    """注册并按稳定优先级顺序执行同步 Hook。"""

    hook_errors_key: ClassVar[str] = "hook_errors"  # 非关键 Hook 错误的元数据键。

    def __init__(self) -> None:
        """初始化空 Hook 注册表。

        Returns:
            None。
        """
        self._hooks: dict[HookEvent, list[_RegisteredHook]] = {}  # 事件到 Hook 列表。
        self._registration_order = 0  # 下一个 Hook 的稳定注册序号。

    def register(
        self,
        event: HookEvent,
        handler: HookHandler,
        *,
        priority: int = 0,
        critical: bool = True,
    ) -> None:
        """为指定生命周期事件注册一个同步处理函数。

        Args:
            event: 需要监听的 HookEvent。
            handler: 接收 HookContext 的同步处理函数。
            priority: 执行优先级，数值越大越先执行。
            critical: 处理函数失败时是否中止当前核心流程。

        Returns:
            None。

        Raises:
            TypeError: 事件、处理函数或注册参数类型无效。
        """
        if not isinstance(event, HookEvent):
            raise TypeError("event 必须是 HookEvent")
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError("priority 必须是整数")
        if not isinstance(critical, bool):
            raise TypeError("critical 必须是布尔值")

        registration = _RegisteredHook(
            handler=handler,
            priority=priority,
            critical=critical,
            order=self._registration_order,
        )
        self._registration_order += 1
        hooks = self._hooks.setdefault(event, [])
        hooks.append(registration)
        hooks.sort(key=lambda item: (-item.priority, item.order))

    def emit(self, context: HookContext) -> HookContext:
        """执行当前事件注册的全部 Hook，并返回同一个上下文。

        Args:
            context: 当前生命周期事件的数据上下文。

        Returns:
            执行 Hook 后的同一个 HookContext，before Hook 可修改 payload。

        Raises:
            TypeError: context 不是 HookContext 或处理函数返回异步对象。
            HookRejectedError: 任一 Hook 主动拒绝继续执行。
            HookExecutionError: critical Hook 执行失败。
        """
        if not isinstance(context, HookContext):
            raise TypeError("context 必须是 HookContext")

        for registration in tuple(self._hooks.get(context.event, [])):
            handler_name = self._handler_name(registration.handler)
            try:
                result = registration.handler(context)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError(
                        f"Hook {handler_name!r} 返回了异步对象，当前仅支持同步 Hook"
                    )
            except HookRejectedError:
                raise
            except Exception as error:
                if registration.critical:
                    raise HookExecutionError(
                        f"关键 Hook 执行失败: {context.event.value}/{handler_name}"
                    ) from error
                context.metadata.setdefault(self.hook_errors_key, []).append(
                    {
                        "event": context.event.value,
                        "hook": handler_name,
                        "error": str(error),
                    }
                )
                _LOGGER.warning(
                    "非关键 Hook 执行失败: %s/%s: %s",
                    context.event.value,
                    handler_name,
                    error,
                )
        return context

    def clear(self, event: HookEvent | None = None) -> None:
        """清空全部 Hook，或只清空指定事件的 Hook。

        Args:
            event: 可选的目标事件；省略时清空全部注册。

        Returns:
            None。

        Raises:
            TypeError: event 既不是 HookEvent 也不是 None。
        """
        if event is None:
            self._hooks.clear()
            return
        if not isinstance(event, HookEvent):
            raise TypeError("event 必须是 HookEvent 或 None")
        self._hooks.pop(event, None)

    @staticmethod
    def _handler_name(handler: HookHandler) -> str:
        """返回适合错误信息和日志使用的 Hook 名称。

        Args:
            handler: 已注册的 Hook 处理函数。

        Returns:
            函数名或可调用对象的类名。
        """
        return getattr(handler, "__name__", handler.__class__.__name__)
