"""工具引擎，负责管理工具定义、注册本地处理函数并执行工具。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

ToolHandler = Callable[..., Any]


class ToolEngine:
    """集中管理可供模型调用的工具及其本地实现。"""

    def __init__(self) -> None:
        """初始化空工具集合。

        Returns:
            None。
        """
        self._definitions: dict[str, dict[str, Any]] = {}  # 工具名称到模型工具定义的映射。
        self._handlers: dict[str, ToolHandler] = {}  # 工具名称到本地处理函数的映射。

    def register(
        self,
        definition: Mapping[str, Any],
        handler: ToolHandler,
    ) -> None:
        """注册一个模型工具定义及其本地处理函数。

        Args:
            definition: OpenAI function tool 格式的工具定义。
            handler: 接收工具参数并返回执行结果的本地函数。

        Returns:
            None。

        Raises:
            ValueError: 工具定义中没有有效的函数名称。
        """
        function = definition.get("function")
        if not isinstance(function, Mapping) or not function.get("name"):
            raise ValueError("工具定义缺少 function.name")

        name = str(function["name"])
        self._definitions[name] = dict(definition)
        self._handlers[name] = handler

    def get_definitions(self) -> list[dict[str, Any]]:
        """返回当前已注册的模型工具定义。

        Returns:
            可发送给模型的工具定义列表副本。
        """
        return [dict(definition) for definition in self._definitions.values()]

    def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """执行指定名称的工具。

        Args:
            name: 已注册的工具名称。
            arguments: 传递给工具处理函数的参数。

        Returns:
            工具处理函数返回的执行结果。

        Raises:
            KeyError: 请求的工具尚未注册。
        """
        if name not in self._handlers:
            raise KeyError(f"未注册工具: {name}")

        # TODO: 后续根据运行需求补充工具权限、超时和失败处理策略。
        return self._handlers[name](**dict(arguments))
