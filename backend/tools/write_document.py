"""写入文档工具骨架，具体文档生成逻辑将在后续实现。"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.tools.base import BaseTool, ToolImpact


class WriteDocumentTool(BaseTool):
    """声明写入文档所需的模型工具接口。"""

    name: ClassVar[str] = "write_document"  # 模型调用写入文档功能时使用的工具名称。
    description: ClassVar[str] = "将内容写入指定路径的文档。"  # 提供给模型的功能说明。
    impact: ClassVar[ToolImpact] = ToolImpact.WRITE  # 写入操作会改变文档状态。
    parameters: ClassVar[dict[str, Any]] = {  # 写入文档工具的输入参数定义。
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "需要写入的文档路径。",
            },
            "content": {
                "type": "string",
                "description": "需要写入文档的内容。",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def execute(self, **arguments: Any) -> str:
        """将指定内容写入文档并返回执行结果。

        Args:
            **arguments: 包含文档路径和写入内容的工具调用参数。

        Returns:
            描述文档写入结果的文本。

        Raises:
            NotImplementedError: 当前仅提供工具骨架，尚未实现文档写入。
        """
        # TODO: 明确文档格式与覆盖策略后，实现安全的文档写入。
        raise NotImplementedError("文档写入工具尚未实现")
