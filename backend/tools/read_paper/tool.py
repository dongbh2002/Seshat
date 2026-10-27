"""论文正文阅读工具：接受论文标识或公开链接，以有界片段与版本游标返回原文。"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.config import PaperSettings
from backend.papers.reader import PaperReader
from backend.tools.base import BaseTool, ToolImpact


class ReadPaperTool(BaseTool):
    """读取公开论文的文本与定位信息，不修改用户论文或写入长期记忆。"""

    name: ClassVar[str] = "read_paper"  # 模型调用论文正文阅读的名称。
    description: ClassVar[str] = (  # 模型可见的阅读边界。
        "读取 search_papers 返回的 paper_id、DOI 或公开 PDF/HTML 链接。"
        "返回来源、内容版本、页码/段落和续读游标。"
        "依据 read_status 与 limitations 判断是否为正文、部分内容或仅摘要；"
        "eof 只表示已提取内容读到末尾，不保证源论文完整。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.READ_ONLY  # 只读论文，缓存不改变源内容。

    def __init__(self, reader: PaperReader, settings: PaperSettings) -> None:
        """初始化论文阅读工具。

        Args:
            reader: 按当前身份缓存正文的阅读器。
            settings: 字符上限与工具超时配置。

        Returns:
            None。
        """
        self.reader = reader  # 正文下载、解析与续读组件。
        self.settings = settings  # 显式读取配置。
        self.timeout_seconds = settings.tool_timeout_seconds  # 工具整体超时。
        self.parameters = {  # 按运行配置生成的阅读输入定义。
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "论文 paper_id、DOI 或公开 PDF/HTML/论文页面链接。",
                },
                "cursor": {
                    "type": "string",
                    "description": "上次 read_paper 返回的 next_cursor；省略时从头读取。",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": settings.read_max_chars,
                    "description": f"本次正文的字符上限；省略时为 {settings.read_default_max_chars}。",
                },
            },
            "required": ["reference"],
            "additionalProperties": False,
        }

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """读取一段论文原文。

        Args:
            **arguments: reference，以及可选 cursor、max_chars。

        Returns:
            阅读器的题录、来源、版本、读取状态、正文块、续读标识及解析局限；
            各字段含义见 PaperReader.read。

        Raises:
            ValueError: 标识、游标、字符上限或内容版本无效。
        """
        reference = arguments.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("reference 必须是非空论文标识或公开地址")
        cursor = arguments.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("cursor 必须使用上次返回的 next_cursor")
        max_chars = arguments.get("max_chars", self.settings.read_default_max_chars)
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or not 1 <= max_chars <= self.settings.read_max_chars
        ):
            raise ValueError(
                f"max_chars 必须在 1 到 {self.settings.read_max_chars} 之间"
            )
        return self.reader.read(reference.strip(), cursor=cursor, max_chars=max_chars)
