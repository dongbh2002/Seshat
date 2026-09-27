"""定义按条件查询完整审阅问题列表的 Agent 工具。"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.session import FINDING_STATUSES, ReviewStateStore
from backend.tools.base import BaseTool, ToolImpact


class ListFindingsTool(BaseTool):
    """返回完整问题列表，弥补上下文中审阅状态只展示部分问题的限制。"""

    name: ClassVar[str] = "list_findings"  # 模型调用问题查询时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的问题查询说明。
        "按状态或文档查询完整的审阅问题列表，包括上下文中未展示的问题。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.READ_ONLY  # 只读取审阅状态。
    timeout_seconds: ClassVar[float] = 10.0  # 内存查询的超时时间。
    parameters: ClassVar[dict[str, Any]] = {  # 问题查询工具的输入参数定义。
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": list(FINDING_STATUSES),
                "description": "只返回该状态的问题；省略时返回全部状态。",
            },
            "path": {
                "type": "string",
                "description": "只返回该文档的问题；省略时返回全部文档。",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, store: ReviewStateStore) -> None:
        """初始化问题查询工具。

        Args:
            store: 被查询的审阅状态存储。

        Returns:
            None。
        """
        self.store = store  # 被查询的审阅状态存储。

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """按条件过滤问题。

        Args:
            **arguments: 可选的 status 和 path。

        Returns:
            含 findings（id、path、block_id、issue、status）的字典。
        """
        status = arguments.get("status")
        path = arguments.get("path")
        return {
            "findings": [
                finding
                for finding in self.store.snapshot()["findings"]
                if (status is None or finding["status"] == status)
                and (path is None or finding["path"] == path)
            ]
        }
