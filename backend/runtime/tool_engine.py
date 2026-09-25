"""工具引擎，负责管理工具定义、注册本地处理函数并执行工具。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from backend.runtime.hook_engine import (
    HookContext,
    HookEngine,
    HookEvent,
    HookScope,
)
from backend.tools.base import BaseTool


class ToolEngine:
    """集中管理可供模型调用的工具及其本地实现。"""

    def __init__(self, hook_engine: HookEngine | None = None) -> None:
        """初始化空工具集合及可选的生命周期 Hook。

        Args:
            hook_engine: 工具执行前后和失败时使用的共享 HookEngine。

        Returns:
            None。
        """
        self._tools: dict[str, BaseTool] = {}  # 工具名称到完整工具对象的映射。
        self.hook_engine = hook_engine  # 可选的共享生命周期 Hook 执行入口。

    def register(self, tool: BaseTool) -> None:
        """注册一个实现统一接口的工具对象。

        Args:
            tool: 提供模型工具定义、影响类型和执行方法的工具实例。

        Returns:
            None。

        Raises:
            ValueError: 工具没有有效名称。
        """
        if not tool.name:
            raise ValueError("工具名称不能为空")

        self._tools[tool.name] = tool

    def get_definitions(self) -> list[dict[str, Any]]:
        """返回当前已注册的模型工具定义。

        Returns:
            可发送给模型的工具定义列表副本。
        """
        return [tool.get_definition() for tool in self._tools.values()]

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        hook_scope: HookScope | None = None,
        hook_metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """执行指定名称的工具。

        Args:
            name: 已注册的工具名称。
            arguments: 传递给工具处理函数的参数。
            hook_scope: 可选的租户、用户、会话和运行范围。
            hook_metadata: 可选的模型步骤和工具调用等扩展信息。

        Returns:
            工具处理函数返回的执行结果。

        Raises:
            KeyError: 请求的工具尚未注册。
            TimeoutError: 工具执行时间超过自身配置的超时时间。
            ValueError: 工具配置的超时时间不是正数。
        """
        scope = hook_scope or HookScope()
        metadata = dict(hook_metadata or {})
        payload = {
            "name": name,
            "arguments": copy.deepcopy(dict(arguments)),
        }
        executor: ThreadPoolExecutor | None = None
        executed_payload = payload
        try:
            if name not in self._tools:
                raise KeyError(f"未注册工具: {name}")

            tool = self._tools[name]
            payload.update(
                {
                    "impact": tool.impact.value,
                    "timeout_seconds": tool.timeout_seconds,
                }
            )
            if tool.timeout_seconds <= 0:
                raise ValueError(f"工具超时时间必须为正数: {name}")

            before_context = self._emit_hook(
                HookEvent.BEFORE_TOOL_EXECUTE,
                scope=scope,
                payload=payload,
                metadata=metadata,
            )
            executed_payload = before_context.payload
            hooked_arguments = before_context.payload.get("arguments")
            if not isinstance(hooked_arguments, Mapping):
                raise ValueError("before_tool_execute 必须保留对象类型的 arguments")

            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"tool-{name}",
            )
            future = executor.submit(tool.execute, **dict(hooked_arguments))
            try:
                result = future.result(timeout=tool.timeout_seconds)
            except FutureTimeoutError as error:
                future.cancel()
                raise TimeoutError(
                    f"工具执行超时: {name}（{tool.timeout_seconds:g} 秒）"
                ) from error

            self._emit_hook(
                HookEvent.AFTER_TOOL_EXECUTE,
                scope=scope,
                payload=executed_payload,
                metadata=metadata,
                result=result,
            )
            return result
        except Exception as error:
            try:
                self._emit_hook(
                    HookEvent.TOOL_ERROR,
                    scope=scope,
                    payload=executed_payload,
                    metadata=metadata,
                    error=error,
                )
            except Exception as hook_error:
                raise hook_error from error
            raise
        finally:
            # TODO: 若工具需要强制终止，后续应改为支持取消的进程或异步执行模型。
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    def _emit_hook(
        self,
        event: HookEvent,
        *,
        scope: HookScope,
        payload: dict[str, Any],
        metadata: dict[str, Any],
        result: Any = None,
        error: BaseException | None = None,
    ) -> HookContext:
        """构造工具生命周期上下文，并在已配置时触发 Hook。

        Args:
            event: 当前工具生命周期事件。
            scope: 当前运行范围。
            payload: 工具名称、参数、影响类型和超时配置。
            metadata: 本次工具调用中各 Hook 共享的扩展数据。
            result: 工具成功执行后的结果。
            error: 工具执行或 Hook 失败时的异常。

        Returns:
            Hook 执行后的上下文；未配置 HookEngine 时原样返回。
        """
        context = HookContext(
            event=event,
            scope=scope,
            payload=payload,
            metadata=metadata,
            result=result,
            error=error,
        )
        if self.hook_engine is not None:
            self.hook_engine.emit(context)
        return context
