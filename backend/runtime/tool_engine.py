"""工具引擎，负责管理工具定义、注册本地处理函数并执行工具。"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from backend.tools.base import BaseTool


class ToolEngine:
    """集中管理可供模型调用的工具及其本地实现。"""

    def __init__(self) -> None:
        """初始化空工具集合。

        Returns:
            None。
        """
        self._tools: dict[str, BaseTool] = {}  # 工具名称到完整工具对象的映射。

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

    def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """执行指定名称的工具。

        Args:
            name: 已注册的工具名称。
            arguments: 传递给工具处理函数的参数。

        Returns:
            工具处理函数返回的执行结果。

        Raises:
            KeyError: 请求的工具尚未注册。
            TimeoutError: 工具执行时间超过自身配置的超时时间。
            ValueError: 工具配置的超时时间不是正数。
        """
        if name not in self._tools:
            raise KeyError(f"未注册工具: {name}")

        tool = self._tools[name]
        if tool.timeout_seconds <= 0:
            raise ValueError(f"工具超时时间必须为正数: {name}")

        # TODO: 后续根据 tool.impact 补充写操作确认、权限和失败处理策略。
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        future = executor.submit(tool.execute, **dict(arguments))
        try:
            return future.result(timeout=tool.timeout_seconds)
        except FutureTimeoutError as error:
            future.cancel()
            raise TimeoutError(
                f"工具执行超时: {name}（{tool.timeout_seconds:g} 秒）"
            ) from error
        finally:
            # TODO: 若工具需要强制终止，后续应改为支持取消的进程或异步执行模型。
            executor.shutdown(wait=False, cancel_futures=True)
