"""工具抽象基类，统一模型工具定义和本地执行接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar


class ToolImpact(str, Enum):
    """描述工具执行时是否会改变外部状态。"""

    READ_ONLY = "read_only"  # 工具只读取数据，不改变外部状态。
    WRITE = "write"  # 工具会创建或修改外部状态。


class BaseTool(ABC):
    """定义可被工具引擎注册的统一工具接口。"""

    name: ClassVar[str]  # 模型调用工具时使用的唯一名称。
    description: ClassVar[str]  # 提供给模型的工具用途说明。
    parameters: ClassVar[dict[str, Any]]  # 工具输入参数的 JSON Schema。
    impact: ClassVar[ToolImpact]  # 工具对外部状态产生的影响类型。
    timeout_seconds: ClassVar[float] = 300.0  # 工具执行的默认超时时间，单位为秒。

    def get_definition(self) -> dict[str, Any]:
        """生成供模型调用的 function tool 定义。

        Returns:
            符合 OpenAI function tool 格式的工具定义。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def execute(self, **arguments: Any) -> Any:
        """执行工具功能。

        Args:
            **arguments: 模型按照工具参数定义生成的调用参数。

        Returns:
            可返回给模型的工具执行结果。
        """
        raise NotImplementedError
