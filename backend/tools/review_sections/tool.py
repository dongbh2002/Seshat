"""定义按章节 map-reduce 审阅论文的 Agent 工具。

map：每个章节由 SectionReviewer 在独立上下文中审阅；
reduce：问题写入 ReviewStateStore，主对话只看到问题编号，由主模型汇总。
"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.llm_tasks import SectionReviewer
from backend.memory import MemorySource
from backend.session import ReviewStateStore
from backend.tools.base import ToolImpact
from backend.tools.section_tool import SectionTool
from backend.utils.docx import DocumentIndex


class ReviewSectionsTool(SectionTool):
    """逐章独立审阅，主对话上下文开销与论文长度基本无关。"""

    name: ClassVar[str] = "review_sections"  # 模型调用章节审阅时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的章节审阅说明。
        "按章节独立审阅 DOCX，问题自动记入审阅工作状态并标记章节已审阅。"
        "适合通读全文或多个章节；返回问题编号，问题详情见审阅工作状态。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.WRITE  # 写入会话审阅状态。
    timeout_seconds: ClassVar[float] = 1800.0  # 多章节串行调用模型的超时时间。
    parameters: ClassVar[dict[str, Any]] = {  # 章节审阅工具的输入参数定义。
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于允许工作目录的 DOCX 路径。",
            },
            "focus": {
                "type": "string",
                "description": "审阅要求，例如“逻辑与论证”“学术表达”。",
            },
            "section_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "需要审阅的章节 ID；省略时审阅全部未完成章节。",
            },
        },
        "required": ["path", "focus"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        document_index: DocumentIndex,
        reviewer: SectionReviewer,
        store: ReviewStateStore,
        memory: MemorySource | None,
    ) -> None:
        """初始化章节审阅工具。

        Args:
            document_index: 提供章节划分的文档结构索引。
            reviewer: 在独立上下文中审阅单个章节的子任务。
            store: 写入问题和审阅进度的审阅状态存储。
            memory: 长期记忆来源，审阅时传给子任务；未启用记忆时为 None。

        Returns:
            None。
        """
        super().__init__(document_index)
        self.reviewer = reviewer  # 单章节审阅子任务。
        self.store = store  # 审阅结果写入的状态存储。
        self.memory = memory  # 长期记忆来源。

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """逐章审阅并把结果写入审阅状态。

        Args:
            **arguments: path、focus 和可选 section_ids。

        Returns:
            path、revision、sections（id、title、finding_ids 或 error）
            以及被丢弃问题的 warnings。

        Raises:
            ValueError: focus 为空或 path、section_ids 无效。
        """
        focus = arguments.get("focus")
        if not isinstance(focus, str) or not focus.strip():
            raise ValueError("focus 必须是非空字符串")
        snapshot, sections = self._load_sections(arguments)
        self.store.track_document(snapshot.path, snapshot.revision)
        if arguments.get("section_ids") is None:
            sections = [
                section
                for section in sections
                if self.store.get_unreviewed(snapshot.path, section.block_ids)
            ]
        decisions = self.store.snapshot()["decisions"]
        memory = (
            self.memory.rules_for_review(snapshot.revision)
            if self.memory is not None
            else []
        )

        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        for section in sections:
            entry: dict[str, Any] = {"id": section.id, "title": section.title}
            try:
                findings = self.reviewer.review(
                    section.markdown,
                    focus=focus.strip(),
                    decisions=decisions,
                    memory=memory,
                )
            except Exception as error:  # noqa: BLE001 - 单章失败不影响其他章节。
                entry["error"] = str(error)
                results.append(entry)
                continue
            finding_ids: list[str] = []
            for finding in findings:
                if finding["block_id"] not in section.block_ids:
                    warnings.append(
                        f"{section.id} 返回了章节外的块 ID {finding['block_id']}，已丢弃"
                    )
                    continue
                finding_ids.append(
                    self.store.add_finding(
                        snapshot.path,
                        finding["block_id"],
                        finding["issue"],
                        excerpt=snapshot.get_block_text(finding["block_id"]),
                    )
                )
            self.store.mark_reviewed(
                snapshot.path, snapshot.get_block_texts(section.block_ids)
            )
            entry["finding_ids"] = finding_ids
            results.append(entry)
        # TODO: 章节较多时改为并发调用。
        return {
            "path": snapshot.path,
            "revision": snapshot.revision,
            "sections": results,
            "warnings": warnings,
        }
