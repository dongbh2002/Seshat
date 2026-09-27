"""定义按章节获取论文摘要的 Agent 工具。"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.llm_tasks import SectionSummarizer
from backend.tools.base import ToolImpact
from backend.tools.section_tool import SectionTool
from backend.utils.docx import DocumentIndex


class SummarizeSectionsTool(SectionTool):
    """返回章节摘要，摘要按章节内容缓存，内容不变不重复生成。"""

    name: ClassVar[str] = "summarize_sections"  # 模型调用章节摘要时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的章节摘要说明。
        "按章节返回 DOCX 摘要，用于快速了解全文或定位章节，不含原文细节。"
        "章节 ID 见审阅工作状态中的大纲。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.READ_ONLY  # 只读文档，缓存不影响文档。
    timeout_seconds: ClassVar[float] = 1800.0  # 多章节串行调用模型的超时时间。
    parameters: ClassVar[dict[str, Any]] = {  # 章节摘要工具的输入参数定义。
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于允许工作目录的 DOCX 路径。",
            },
            "section_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "需要摘要的章节 ID；省略时返回全部章节。",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        document_index: DocumentIndex,
        summarizer: SectionSummarizer,
    ) -> None:
        """初始化章节摘要工具。

        Args:
            document_index: 提供章节划分的文档结构索引。
            summarizer: 生成并缓存章节摘要的子任务。

        Returns:
            None。
        """
        super().__init__(document_index)
        self.summarizer = summarizer  # 章节摘要子任务。

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """生成或读取所选章节的摘要。

        Args:
            **arguments: path 和可选 section_ids。

        Returns:
            path、revision 和 sections（id、title、summary 或 error）。
        """
        snapshot, sections = self._load_sections(arguments)
        results: list[dict[str, Any]] = []
        for section in sections:
            entry: dict[str, Any] = {"id": section.id, "title": section.title}
            try:
                entry["summary"] = self.summarizer.summarize(section.markdown)
            except Exception as error:  # noqa: BLE001 - 单章失败不影响其他章节。
                entry["error"] = str(error)
            results.append(entry)
        # TODO: 章节较多时改为并发调用。
        return {
            "path": snapshot.path,
            "revision": snapshot.revision,
            "sections": results,
        }
