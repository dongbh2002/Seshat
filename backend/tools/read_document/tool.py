"""定义面向 Agent 的 DOCX 读取工具及其调用边界。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from zipfile import BadZipFile, ZipFile

from backend.tools.base import BaseTool, ToolImpact
from backend.utils.docx import (
    NAMESPACES,
    DocumentBlock,
    DocxParser,
    calculate_revision,
    resolve_docx_path,
)


class ReadDocumentTool(BaseTool):
    """安全读取工作目录内的 DOCX，并返回 Markdown 和 XML 定位索引。"""

    name: ClassVar[str] = "read_document"  # 模型调用文档读取功能时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的文档读取功能说明。
        "读取 DOCX 的结构化 Markdown，可按稳定 ID 分块续读；"
        "用 start_id 和 end_id 读取指定范围（如某一章或单个块）。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.READ_ONLY  # 读取操作不改变文档状态。
    timeout_seconds: ClassVar[float] = 300.0  # DOCX 读取的默认超时时间。
    min_character_limit: ClassVar[int] = 1_000  # 允许的最小返回字符上限。

    def __init__(
        self,
        root_directory: Path,
        *,
        default_max_chars: int,
        max_chars_limit: int,
    ) -> None:
        """初始化 DOCX 读取工具。

        Args:
            root_directory: 工具允许读取的根目录。
            default_max_chars: 模型未指定 max_chars 时单次返回的字符上限。
            max_chars_limit: 模型可指定的 max_chars 最大值。

        Returns:
            None。

        Raises:
            NotADirectoryError: 指定根目录不存在或不是目录。
            ValueError: 字符上限不满足 最小值 <= 默认值 <= 最大值。
        """
        resolved_root = root_directory.resolve()
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"文档根目录不存在: {resolved_root}")
        if not self.min_character_limit <= default_max_chars <= max_chars_limit:
            raise ValueError(
                f"读取字符上限须满足 {self.min_character_limit} <= "
                f"default_max_chars({default_max_chars}) <= "
                f"max_chars_limit({max_chars_limit})"
            )
        self.root_directory = resolved_root  # 工具允许访问的已解析根目录。
        self.default_max_chars = default_max_chars  # 未指定时单次返回的字符上限。
        self.max_chars_limit = max_chars_limit  # 单次返回允许的最大字符数。
        self.parameters = self._build_parameters()  # 按实例上限生成的参数定义。

    def _build_parameters(self) -> dict[str, Any]:
        """生成带本实例字符上限的输入参数 JSON Schema。

        Returns:
            read_document 的参数定义。
        """
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于允许工作目录的 DOCX 路径。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["accepted", "markup"],
                    "default": "accepted",
                    "description": "接受修订后的文本，或显式显示插入和删除标记。",
                },
                "view": {
                    "type": "string",
                    "enum": ["content", "outline"],
                    "default": "content",
                    "description": "读取正文内容或仅返回标题大纲。",
                },
                "start_id": {
                    "type": "string",
                    "description": "从指定内容块 ID 开始读取；省略时从文档开头读取。",
                },
                "end_id": {
                    "type": "string",
                    "description": "读到指定内容块 ID 为止（包含该块）；省略时读到文档末尾。",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": self.min_character_limit,
                    "maximum": self.max_chars_limit,
                    "default": self.default_max_chars,
                    "description": "单次返回 Markdown 的字符上限，按完整内容块截断。",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """读取 DOCX 并返回可供模型阅读和后续修改定位的结果。

        Args:
            **arguments: 文档路径、修订模式、视图、起止 ID 和字符上限。

        Returns:
            Markdown（含块 ID 与内嵌批注）、带位置的结构索引、版本、与本次
            正文相关的附加内容、统计信息、续读 ID，以及读取正文时提示模型
            标记审阅范围的 review_hint。

        Raises:
            ValueError: 参数、文件格式或 DOCX 内部结构无效。
            PermissionError: 文档路径超出允许根目录。
            FileNotFoundError: 指定文档不存在。
        """
        path_argument = arguments.get("path")
        if not isinstance(path_argument, str) or not path_argument.strip():
            raise ValueError("文档路径必须是非空字符串")

        mode = arguments.get("mode", "accepted")
        if mode not in {"accepted", "markup"}:
            raise ValueError("mode 必须是 accepted 或 markup")
        view = arguments.get("view", "content")
        if view not in {"content", "outline"}:
            raise ValueError("view 必须是 content 或 outline")

        start_id = arguments.get("start_id")
        if start_id is not None and (
            not isinstance(start_id, str) or not start_id.strip()
        ):
            raise ValueError("start_id 必须是非空字符串")
        end_id = arguments.get("end_id")
        if end_id is not None and (not isinstance(end_id, str) or not end_id.strip()):
            raise ValueError("end_id 必须是非空字符串")
        max_chars = arguments.get("max_chars", self.default_max_chars)
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or not self.min_character_limit <= max_chars <= self.max_chars_limit
        ):
            raise ValueError("max_chars 超出范围")

        document_path = resolve_docx_path(self.root_directory, path_argument)
        try:
            with ZipFile(document_path) as archive:
                parser = DocxParser(archive)
                all_blocks = parser.parse_blocks(mode)
                candidate_blocks = (
                    [block for block in all_blocks if block.kind == "heading"]
                    if view == "outline"
                    else all_blocks
                )
                returned_blocks, markdown, next_id = self._select_blocks(
                    candidate_blocks,
                    start_id=start_id,
                    end_id=end_id,
                    max_chars=max_chars,
                )
                extras = self._select_extras(
                    parser.read_extras(mode),
                    markdown,
                    from_document_start=start_id is None and view == "content",
                )
                warnings = parser.get_warnings()
        except BadZipFile as error:
            raise ValueError(f"文件不是有效的 DOCX: {path_argument}") from error

        relative_path = document_path.relative_to(self.root_directory).as_posix()
        result: dict[str, Any] = {
            "path": relative_path,
            "revision": calculate_revision(document_path),
            "mode": mode,
            "view": view,
            "markdown": markdown,
            "index": self._build_index(returned_blocks),
            "next_id": next_id,
            "extras": extras,
            "metadata": {
                "block_count": len(all_blocks),
                "returned_block_count": len(returned_blocks),
                "heading_count": sum(block.kind == "heading" for block in all_blocks),
                "table_count": sum(block.kind == "table" for block in all_blocks),
                "changed_block_count": sum(block.has_changes for block in all_blocks),
                "section_count": len(
                    parser.document.findall(".//w:sectPr", NAMESPACES)
                ),
                "drawing_count": len(
                    parser.document.findall(".//w:drawing", NAMESPACES)
                ),
                "truncated": next_id is not None,
            },
            "warnings": warnings,
        }
        if view == "content" and returned_blocks:
            result["review_hint"] = (
                "读完后请调用 update_review_state："
                f"mark_reviewed start_id={returned_blocks[0].block_id} "
                f"end_id={returned_blocks[-1].block_id}，发现问题同时 add_finding；"
                "未标记的正文在上下文紧张时无法清理。"
            )
        return result

    @staticmethod
    def _select_extras(
        extras: dict[str, list[dict[str, str]]],
        markdown: str,
        *,
        from_document_start: bool,
    ) -> dict[str, list[dict[str, str]]]:
        """只返回与本次读取相关的正文外内容，避免分页读取时重复。

        脚注、尾注只保留正文中出现引用标记（[脚注:N] / [尾注:N]）的条目；
        页眉页脚只在从文档开头读取正文时返回。

        Args:
            extras: 解析器读取的全部页眉、页脚、脚注和尾注。
            markdown: 本次返回的正文。
            from_document_start: 是否从文档开头读取正文。

        Returns:
            筛选后的附加内容，没有内容的类别不出现。
        """
        selected: dict[str, list[dict[str, str]]] = {}
        for key, marker in (("footnotes", "脚注"), ("endnotes", "尾注")):
            referenced = [
                item
                for item in extras.get(key, [])
                if f"[{marker}:{item.get('id')}]" in markdown
            ]
            if referenced:
                selected[key] = referenced
        if from_document_start:
            for key in ("headers", "footers"):
                if extras.get(key):
                    selected[key] = extras[key]
        return selected

    @staticmethod
    def _build_index(blocks: list[DocumentBlock]) -> list[dict[str, Any]]:
        """生成返回块的结构索引，并记录每块在 markdown 中的位置。

        markdown 由各块以两个换行拼接，offset / length 供上下文压缩按块精确
        清理正文。

        Args:
            blocks: 本次返回的内容块。

        Returns:
            每块的索引字典，附带 offset（起始字符位置）与 length（字符数）。
        """
        index: list[dict[str, Any]] = []
        offset = 0
        for block in blocks:
            length = len(block.to_markdown())
            index.append({**block.to_index(), "offset": offset, "length": length})
            offset += length + 2
        return index

    @staticmethod
    def _select_blocks(
        blocks: list[DocumentBlock],
        *,
        start_id: str | None,
        end_id: str | None,
        max_chars: int,
    ) -> tuple[list[DocumentBlock], str, str | None]:
        """在 start_id 到 end_id（含两端）范围内按字符上限选择完整内容块。

        Args:
            blocks: 当前视图下可读取的内容块。
            start_id: 可选的起始内容块 ID；省略时从第一块开始。
            end_id: 可选的结束内容块 ID；省略时到最后一块为止。
            max_chars: 单次 Markdown 返回字符上限。

        Returns:
            返回块、Markdown 文本和续读 ID 组成的元组；续读 ID 指向范围内
            下一块，范围已全部返回时为 None。

        Raises:
            ValueError: 起止 ID 不存在于当前视图，或起始块位于结束块之后。
        """
        block_ids = [block.block_id for block in blocks]
        for block_id in (start_id, end_id):
            if block_id is not None and block_id not in block_ids:
                raise ValueError(f"当前视图中不存在内容块 ID: {block_id}")
        if not blocks:
            return [], "", None
        start_index = block_ids.index(start_id) if start_id is not None else 0
        end_index = block_ids.index(end_id) if end_id is not None else len(blocks) - 1
        if start_index > end_index:
            raise ValueError(f"start_id {start_id} 位于 end_id {end_id} 之后")

        returned_blocks: list[DocumentBlock] = []
        markdown_parts: list[str] = []
        next_id: str | None = None
        current_length = 0
        for block in blocks[start_index : end_index + 1]:
            block_markdown = block.to_markdown()
            added_length = len(block_markdown) + (2 if markdown_parts else 0)
            if returned_blocks and current_length + added_length > max_chars:
                next_id = block.block_id
                break
            returned_blocks.append(block)
            markdown_parts.append(block_markdown)
            current_length += added_length

        return returned_blocks, "\n\n".join(markdown_parts), next_id
