"""章节类工具基类，统一文档加载和章节选择，供 summarize_sections、review_sections 继承。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from backend.tools.base import BaseTool
from backend.utils.docx import (
    DocumentIndex,
    DocumentSection,
    DocumentSnapshot,
)


class SectionTool(BaseTool):
    """基于 DocumentIndex 按章节处理文档的工具基类。"""

    def __init__(self, document_index: DocumentIndex) -> None:
        """初始化章节类工具。

        Args:
            document_index: 提供章节划分的文档结构索引。

        Returns:
            None。
        """
        self.document_index = document_index  # 提供章节划分的文档结构索引。

    def _load_sections(
        self,
        arguments: dict[str, Any],
    ) -> tuple[DocumentSnapshot, list[DocumentSection]]:
        """读取文档并按 section_ids 选择章节。

        Args:
            arguments: 含 path 和可选 section_ids 的工具参数。

        Returns:
            文档结构索引与选中章节；未指定 section_ids 时返回全部章节。

        Raises:
            ValueError: path 或 section_ids 无效。
        """
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path 必须是非空字符串")
        snapshot = self.document_index.load(path)
        section_ids = arguments.get("section_ids")
        if section_ids is None:
            return snapshot, list(snapshot.sections)
        if not isinstance(section_ids, Sequence) or isinstance(section_ids, str):
            raise ValueError("section_ids 必须是字符串数组")
        return snapshot, [
            snapshot.get_section(section_id) for section_id in section_ids
        ]
