"""定义面向 Agent 的 DOCX 读取工具及其调用边界。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar
from zipfile import BadZipFile, ZipFile

from backend.tools.base import BaseTool, ToolImpact
from backend.tools.read_document.models import _DocumentBlock
from backend.tools.read_document.parser import _DocxParser, _NAMESPACES


class ReadDocumentTool(BaseTool):
    """安全读取工作目录内的 DOCX，并返回 Markdown 和 XML 定位索引。"""

    name: ClassVar[str] = "read_document"  # 模型调用文档读取功能时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的文档读取功能说明。
        "读取 DOCX 的结构化 Markdown，可按稳定 ID 分块续读。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.READ_ONLY  # 读取操作不改变文档状态。
    timeout_seconds: ClassVar[float] = 300.0  # DOCX 读取的默认超时时间。
    default_max_chars: ClassVar[int] = 20_000  # 单次返回 Markdown 的默认字符上限。
    parameters: ClassVar[dict[str, Any]] = {  # 文档读取工具的输入参数定义。
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
                "description": "从指定内容块 ID 开始续读。",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000,
                "default": 20000,
                "description": "单次返回 Markdown 的字符上限，按完整内容块截断。",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, root_directory: Path) -> None:
        """初始化 DOCX 读取工具。

        Args:
            root_directory: 工具允许读取的根目录。

        Returns:
            None。

        Raises:
            NotADirectoryError: 指定根目录不存在或不是目录。
        """
        resolved_root = root_directory.resolve()
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"文档根目录不存在: {resolved_root}")
        self.root_directory = resolved_root  # 工具允许访问的已解析根目录。

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """读取 DOCX 并返回可供模型阅读和后续修改定位的结果。

        Args:
            **arguments: 文档路径、修订模式、视图、起始 ID 和字符上限。

        Returns:
            Markdown、轻量结构索引、版本、附加部件、统计信息和续读 ID。

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
        max_chars = arguments.get("max_chars", self.default_max_chars)
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or not 1000 <= max_chars <= 100_000
        ):
            raise ValueError("max_chars 超出范围")

        document_path = self._resolve_path(path_argument)
        try:
            with ZipFile(document_path) as archive:
                parser = _DocxParser(archive)
                all_blocks = parser.parse_blocks(mode)
                candidate_blocks = (
                    [block for block in all_blocks if block.kind == "heading"]
                    if view == "outline"
                    else all_blocks
                )
                returned_blocks, markdown, next_id = self._select_blocks(
                    candidate_blocks,
                    start_id=start_id,
                    max_chars=max_chars,
                )
                extras = parser.read_extras(mode)
                warnings = parser.get_warnings()
                comments = list(parser.comments.values())
        except BadZipFile as error:
            raise ValueError(f"文件不是有效的 DOCX: {path_argument}") from error

        relative_path = document_path.relative_to(self.root_directory).as_posix()
        return {
            "path": relative_path,
            "revision": self._calculate_revision(document_path),
            "mode": mode,
            "view": view,
            "markdown": markdown,
            "index": [block.to_index() for block in returned_blocks],
            "next_id": next_id,
            "extras": extras,
            "comments": comments,
            "metadata": {
                "block_count": len(all_blocks),
                "returned_block_count": len(returned_blocks),
                "heading_count": sum(block.kind == "heading" for block in all_blocks),
                "table_count": sum(block.kind == "table" for block in all_blocks),
                "changed_block_count": sum(block.has_changes for block in all_blocks),
                "section_count": len(
                    parser.document.findall(".//w:sectPr", _NAMESPACES)
                ),
                "drawing_count": len(
                    parser.document.findall(".//w:drawing", _NAMESPACES)
                ),
                "truncated": next_id is not None,
            },
            "warnings": warnings,
        }

    def _resolve_path(self, path: str) -> Path:
        """解析并校验工具调用提供的 DOCX 路径。

        Args:
            path: 相对路径或位于允许根目录内的绝对路径。

        Returns:
            已解析且通过访问范围检查的 DOCX 文件路径。

        Raises:
            ValueError: 文件扩展名不是 DOCX。
            PermissionError: 解析后的路径超出允许根目录。
            FileNotFoundError: 路径不存在或不是文件。
        """
        requested_path = Path(path)
        candidate = (
            requested_path
            if requested_path.is_absolute()
            else self.root_directory / requested_path
        )
        resolved_path = candidate.resolve()
        try:
            resolved_path.relative_to(self.root_directory)
        except ValueError as error:
            raise PermissionError(f"禁止访问工作目录外的文档: {path}") from error

        if resolved_path.suffix.lower() != ".docx":
            raise ValueError(f"当前仅支持 DOCX 文档: {path}")
        if not resolved_path.is_file():
            raise FileNotFoundError(f"文档不存在: {path}")
        return resolved_path

    @staticmethod
    def _select_blocks(
        blocks: list[_DocumentBlock],
        *,
        start_id: str | None,
        max_chars: int,
    ) -> tuple[list[_DocumentBlock], str, str | None]:
        """按起始 ID 和字符上限选择完整内容块。

        Args:
            blocks: 当前视图下可读取的内容块。
            start_id: 可选的续读起始内容块 ID。
            max_chars: 单次 Markdown 返回字符上限。

        Returns:
            返回块、Markdown 文本和下一次续读 ID 组成的元组。

        Raises:
            ValueError: 指定的起始 ID 不存在于当前视图。
        """
        start_index = 0
        if start_id is not None:
            block_ids = [block.block_id for block in blocks]
            try:
                start_index = block_ids.index(start_id)
            except ValueError as error:
                raise ValueError(f"当前视图中不存在内容块 ID: {start_id}") from error

        returned_blocks: list[_DocumentBlock] = []
        markdown_parts: list[str] = []
        next_id: str | None = None
        current_length = 0
        for block in blocks[start_index:]:
            block_markdown = block.to_markdown()
            added_length = len(block_markdown) + (2 if markdown_parts else 0)
            if returned_blocks and current_length + added_length > max_chars:
                next_id = block.block_id
                break
            returned_blocks.append(block)
            markdown_parts.append(block_markdown)
            current_length += added_length

        return returned_blocks, "\n\n".join(markdown_parts), next_id

    @staticmethod
    def _calculate_revision(path: Path) -> str:
        """计算 DOCX 内容的 SHA-256 版本标识。

        Args:
            path: 需要计算版本标识的 DOCX 文件路径。

        Returns:
            文档二进制内容对应的十六进制 SHA-256 字符串。
        """
        digest = hashlib.sha256()
        with path.open("rb") as document_file:
            for chunk in iter(lambda: document_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
