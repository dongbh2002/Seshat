"""在线论文检索工具：验证检索参数并返回有界题录，不把摘要当成已读正文。"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.config import PaperSettings
from backend.papers.catalog import PaperSource
from backend.tools.base import BaseTool, ToolImpact


class SearchPapersTool(BaseTool):
    """通过注入的题录源搜索论文，正文由 read_paper 按需读取。"""

    name: ClassVar[str] = "search_papers"  # 模型调用论文检索的名称。
    description: ClassVar[str] = (  # 模型可见的检索边界。
        "按主题或标题关键词搜索论文，返回题录、摘要与公开正文入口。"
        "结果仅为题录，未读取全文；需要方法、实验等原文时调用 read_paper。"
        "续页时沿用相同检索词和筛选条件。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.READ_ONLY  # 只查询外部题录。

    def __init__(self, source: PaperSource, settings: PaperSettings) -> None:
        """初始化检索工具。

        Args:
            source: 可替换的题录检索接口。
            settings: 返回条数、展示长度和工具超时配置。

        Returns:
            None。
        """
        self.source = source  # 题录检索源。
        self.settings = settings  # 显式工具配置。
        self.timeout_seconds = settings.tool_timeout_seconds  # 工具整体超时。
        self.parameters = {  # 按运行配置生成的工具输入定义。
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "主题或标题检索词。"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": settings.search_max_limit,
                    "description": f"本页条数；省略时为 {settings.search_default_limit}。",
                },
                "year_from": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 9999,
                    "description": "包含的起始发表年份。",
                },
                "year_to": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 9999,
                    "description": "包含的结束发表年份。",
                },
                "open_access_only": {
                    "type": "boolean",
                    "description": "是否只检索开放获取论文；省略时不限制。",
                },
                "cursor": {
                    "type": "string",
                    "description": "上次检索的 next_cursor；省略时读取第一页。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """搜索题录。

        Args:
            **arguments: query，以及可选 limit、year_from、year_to、open_access_only、cursor。

        Returns:
            query：检索词；source：检索源；read_status：metadata_only；
            total：匹配总数；papers：各论文全部题录与摘要/作者截断标识；
            next_cursor：下一页标识，末页为 None。

        Raises:
            ValueError: 检索词、年份、条数、筛选条件或游标无效。
            TypeError: open_access_only 不是布尔值。
        """
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 必须是非空检索词")
        limit = arguments.get("limit", self.settings.search_default_limit)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= self.settings.search_max_limit
        ):
            raise ValueError(f"limit 必须在 1 到 {self.settings.search_max_limit} 之间")
        year_from = _year(arguments.get("year_from"), "year_from")
        year_to = _year(arguments.get("year_to"), "year_to")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise ValueError("year_from 不能大于 year_to")
        oa_only = arguments.get("open_access_only", False)
        if not isinstance(oa_only, bool):
            raise TypeError("open_access_only 必须为布尔值")
        cursor = arguments.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
            raise ValueError("cursor 必须使用上次返回的 next_cursor")
        total, papers, next_cursor = self.source.search(
            query.strip(),
            limit=limit,
            cursor=cursor,
            year_from=year_from,
            year_to=year_to,
            open_access_only=oa_only,
        )
        return {
            "query": query.strip(),
            "source": self.settings.source,
            "read_status": "metadata_only",
            "total": total,
            "papers": [paper.metadata(self.settings) for paper in papers],
            "next_cursor": next_cursor,
        }


def _year(value: Any, name: str) -> int | None:
    """校验可选发表年份。

    Args:
        value: 年份或 None。
        name: 错误消息中显示的参数名。

    Returns:
        1 到 9999 之间的整数，未指定为 None。

    Raises:
        ValueError: 年份类型或范围错误。
    """
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 9999:
        raise ValueError(f"{name} 必须在 1 到 9999 之间")
    return value
