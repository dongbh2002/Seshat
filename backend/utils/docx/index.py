"""DOCX 结构索引，提供大纲、按标题划分的章节和内容块范围解析。

索引按 revision 缓存，文档修改后自动重建；供上下文引擎和章节类工具共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from backend.utils.docx.files import calculate_revision, resolve_docx_path
from backend.utils.docx.models import DocumentBlock
from backend.utils.docx.parser import DocxParser


@dataclass(frozen=True)
class DocumentHeading:
    """大纲中的一个标题。"""

    id: str  # 标题内容块 ID。
    level: int  # 标题级别，从 1 开始。
    title: str  # 合并空白后的标题文本。


@dataclass(frozen=True)
class DocumentSection:
    """由不高于指定级别的标题划分出的连续内容块区间。"""

    id: str  # 章节 ID，取章节首个内容块 ID。
    title: str  # 章节标题；首个标题前的内容为空字符串。
    block_ids: tuple[str, ...]  # 章节内按文档顺序排列的内容块 ID。
    markdown: str  # 章节全部内容块的 Markdown，每块带 [ID] 标记。


@dataclass(frozen=True)
class DocumentSnapshot:
    """某一 revision 下的文档结构。"""

    path: str  # 相对于允许根目录的 POSIX 路径。
    revision: str  # 文档内容的 SHA-256。
    headings: tuple[DocumentHeading, ...]  # 全部标题组成的大纲。
    sections: tuple[DocumentSection, ...]  # 按文档顺序排列的章节。
    block_ids: tuple[str, ...]  # 按文档顺序排列的全部内容块 ID。

    def get_section(self, section_id: str) -> DocumentSection:
        """按 ID 查找章节。

        Args:
            section_id: 章节 ID。

        Returns:
            对应的章节。

        Raises:
            ValueError: 章节 ID 不存在。
        """
        for section in self.sections:
            if section.id == section_id:
                return section
        raise ValueError(f"章节 ID 不存在: {section_id}")

    def block_range(self, start_id: str, end_id: str) -> list[str]:
        """返回文档顺序中从起始块到结束块（含）的全部内容块 ID。

        Args:
            start_id: 起始内容块 ID。
            end_id: 结束内容块 ID。

        Returns:
            闭区间内的内容块 ID 列表。

        Raises:
            ValueError: ID 不存在或起始块位于结束块之后。
        """
        try:
            start_index = self.block_ids.index(start_id)
            end_index = self.block_ids.index(end_id)
        except ValueError as error:
            raise ValueError(f"内容块 ID 不存在: {start_id} 或 {end_id}") from error
        if start_index > end_index:
            raise ValueError(f"起始块 {start_id} 位于结束块 {end_id} 之后")
        return list(self.block_ids[start_index : end_index + 1])


class DocumentIndex:
    """解析并缓存允许根目录内 DOCX 的结构索引。"""

    def __init__(self, root_directory: Path, section_heading_level: int) -> None:
        """初始化文档索引。

        Args:
            root_directory: 允许读取的根目录。
            section_heading_level: 划分章节使用的最大标题级别。

        Returns:
            None。

        Raises:
            NotADirectoryError: 根目录不存在。
        """
        resolved_root = root_directory.resolve()
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"文档根目录不存在: {resolved_root}")
        self.root_directory = resolved_root  # 允许访问的已解析根目录。
        self.section_heading_level = section_heading_level  # 章节划分标题级别。
        self._snapshots: dict[str, DocumentSnapshot] = {}  # 路径到最新结构索引。

    def load(self, path: str) -> DocumentSnapshot:
        """读取文档当前 revision 的结构索引，未变化时直接返回缓存。

        Args:
            path: 相对路径或位于根目录内的绝对路径。

        Returns:
            文档结构索引。

        Raises:
            ValueError: 文件不是有效 DOCX。
            PermissionError: 路径超出允许根目录。
            FileNotFoundError: 文档不存在。
        """
        document_path = resolve_docx_path(self.root_directory, path)
        relative_path = document_path.relative_to(self.root_directory).as_posix()
        revision = calculate_revision(document_path)
        cached = self._snapshots.get(relative_path)
        if cached is not None and cached.revision == revision:
            return cached

        try:
            with ZipFile(document_path) as archive:
                blocks = DocxParser(archive).parse_blocks("accepted")
        except BadZipFile as error:
            raise ValueError(f"文件不是有效的 DOCX: {path}") from error
        snapshot = DocumentSnapshot(
            path=relative_path,
            revision=revision,
            headings=tuple(
                DocumentHeading(
                    id=block.block_id,
                    level=block.level,
                    title=" ".join(block.text.split()),
                )
                for block in blocks
                if block.kind == "heading"
            ),
            sections=self._split_sections(blocks),
            block_ids=tuple(block.block_id for block in blocks),
        )
        self._snapshots[relative_path] = snapshot
        return snapshot

    def _split_sections(
        self,
        blocks: list[DocumentBlock],
    ) -> tuple[DocumentSection, ...]:
        """在级别不高于 section_heading_level 的标题处切分章节。

        Args:
            blocks: 按文档顺序排列的内容块。

        Returns:
            章节元组；首个切分标题前的内容自成一个无标题章节。
        """
        groups: list[list[DocumentBlock]] = []
        for block in blocks:
            is_boundary = (
                block.kind == "heading" and block.level <= self.section_heading_level
            )
            if is_boundary or not groups:
                groups.append([])
            groups[-1].append(block)

        return tuple(
            DocumentSection(
                id=group[0].block_id,
                title=(
                    " ".join(group[0].text.split())
                    if group[0].kind == "heading"
                    else ""
                ),
                block_ids=tuple(block.block_id for block in group),
                markdown="\n\n".join(block.to_markdown() for block in group),
            )
            for group in groups
        )
