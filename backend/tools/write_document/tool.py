"""定义基于内容块 ID 安全修改 DOCX 的 Agent 工具。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, ClassVar
from zipfile import BadZipFile

from backend.tools.base import BaseTool, ToolImpact
from backend.tools.write_document.editor import DocumentEditError, DocxEditor


class WriteDocumentTool(BaseTool):
    """校验文档版本，并批量执行可定位的 DOCX 修改操作。"""

    name: ClassVar[str] = "write_document"  # 模型调用文档修改功能时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的文档修改功能说明。
        "按 read_document 返回的内容块 ID 和 revision 修改 DOCX，"
        "支持文本替换、插入段落、删除、设置单元格和添加批注。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.WRITE  # 修改操作会写入外部文档。
    timeout_seconds: ClassVar[float] = 300.0  # DOCX 修改的默认超时时间。
    max_operations: ClassVar[int] = 100  # 单次调用允许执行的最大操作数量。
    parameters: ClassVar[dict[str, Any]] = {  # 文档修改工具的输入参数定义。
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于允许工作目录的源 DOCX 路径。",
            },
            "revision": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{64}$",
                "description": "最近一次 read_document 返回的 SHA-256 revision。",
            },
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "description": "按顺序执行的 DOCX 修改操作。",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "replace_text",
                                "insert_after",
                                "delete",
                                "set_cell",
                                "comment",
                            ],
                            "description": "操作类型。",
                        },
                        "id": {
                            "type": "string",
                            "description": "read_document 返回的内容块 ID。",
                        },
                        "old": {
                            "type": "string",
                            "description": "replace_text 需要匹配的非空原文字。",
                        },
                        "new": {
                            "type": "string",
                            "description": "replace_text 的新文字，可为空。",
                        },
                        "text": {
                            "type": "string",
                            "description": "新段落、单元格或批注文字。",
                        },
                        "occurrence": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "文字多次出现时选择的序号，从 1 开始。",
                        },
                        "style": {
                            "type": "string",
                            "description": "insert_after 可选的 Word 段落 styleId。",
                        },
                        "row": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "set_cell 的物理行下标，从 0 开始。",
                        },
                        "col": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "set_cell 的物理单元格下标，从 0 开始。",
                        },
                        "quote": {
                            "type": "string",
                            "description": "comment 可选的段内原文范围。",
                        },
                    },
                    "required": ["op", "id"],
                    "additionalProperties": False,
                },
            },
            "mode": {
                "type": "string",
                "enum": ["tracked", "direct"],
                "default": "tracked",
                "description": "生成 Word 修订，或直接修改正文 XML。",
            },
            "author": {
                "type": "string",
                "default": "Seshat AI",
                "description": "修订和批注中记录的作者名称。",
            },
            "output_path": {
                "type": "string",
                "description": "可选输出 DOCX 路径；省略时原子覆盖源文档。",
            },
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "另存路径已存在时是否允许覆盖。",
            },
        },
        "required": ["path", "revision", "operations"],
        "additionalProperties": False,
    }

    def __init__(self, root_directory: Path) -> None:
        """初始化 DOCX 修改工具并固定允许访问的根目录。

        Args:
            root_directory: 工具允许读取和写入的根目录。

        Returns:
            None。

        Raises:
            NotADirectoryError: 指定根目录不存在或不是目录。
        """
        resolved_root = root_directory.resolve()
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"文档根目录不存在: {resolved_root}")
        self.root_directory = resolved_root  # 已解析的文档访问边界。

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """校验版本后批量修改 DOCX，并通过临时文件原子保存。

        Args:
            **arguments: 源路径、revision、操作列表和写入选项。

        Returns:
            输出路径、新旧 revision、修改模式和逐项操作结果。

        Raises:
            DocumentEditError: 调用参数、内容块定位或修改语义无效。
            PermissionError: 源或输出路径超出允许根目录。
            FileExistsError: 另存路径已存在但未允许覆盖。
            FileNotFoundError: 源文档不存在。
        """
        source_path = self._resolve_source_path(arguments.get("path"))
        expected_revision = arguments.get("revision")
        if (
            not isinstance(expected_revision, str)
            or len(expected_revision) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in expected_revision
            )
        ):
            raise DocumentEditError("revision 必须是 64 位 SHA-256 十六进制字符串")

        operations = arguments.get("operations")
        if (
            not isinstance(operations, list)
            or not operations
            or len(operations) > self.max_operations
        ):
            raise DocumentEditError(
                f"operations 必须包含 1 到 {self.max_operations} 个操作"
            )

        mode = arguments.get("mode", "tracked")
        if mode not in {"tracked", "direct"}:
            raise DocumentEditError("mode 必须是 tracked 或 direct")
        author = arguments.get("author", "Seshat AI")
        if not isinstance(author, str) or not author.strip() or len(author) > 100:
            raise DocumentEditError("author 必须是长度不超过 100 的非空字符串")
        overwrite = arguments.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise DocumentEditError("overwrite 必须是布尔值")

        output_path = self._resolve_output_path(
            arguments.get("output_path"),
            default_path=source_path,
        )
        if output_path != source_path and output_path.exists() and not overwrite:
            raise FileExistsError(
                f"输出文档已存在，如需覆盖请设置 overwrite=true: {output_path}"
            )

        actual_revision = self._calculate_revision(source_path)
        if actual_revision.lower() != expected_revision.lower():
            raise DocumentEditError(
                "文档自上次读取后已经变化，请重新调用 read_document 获取最新 ID 和 revision"
            )

        temporary_path = self._create_temporary_path(output_path)
        try:
            with DocxEditor(
                source_path,
                author=author.strip(),
                tracked=mode == "tracked",
            ) as editor:
                operation_results = editor.apply(operations)
                editor.save(temporary_path)
            os.replace(temporary_path, output_path)
        except BadZipFile as error:
            raise DocumentEditError(f"文件不是有效的 DOCX: {source_path}") from error
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

        new_revision = self._calculate_revision(output_path)
        return {
            "source_path": source_path.relative_to(self.root_directory).as_posix(),
            "path": output_path.relative_to(self.root_directory).as_posix(),
            "mode": mode,
            "author": author.strip(),
            "previous_revision": actual_revision,
            "revision": new_revision,
            "operation_count": len(operation_results),
            "operations": operation_results,
        }

    def _resolve_source_path(self, path_argument: Any) -> Path:
        """解析并校验源 DOCX 路径。

        Args:
            path_argument: 模型提供的相对或绝对路径参数。

        Returns:
            位于允许根目录内且确实存在的 DOCX 路径。

        Raises:
            DocumentEditError: 路径不是非空字符串或扩展名无效。
            PermissionError: 路径超出允许根目录。
            FileNotFoundError: 源文档不存在。
        """
        if not isinstance(path_argument, str) or not path_argument.strip():
            raise DocumentEditError("path 必须是非空字符串")
        resolved_path = self._resolve_within_root(path_argument)
        if resolved_path.suffix.lower() != ".docx":
            raise DocumentEditError(f"当前仅支持 DOCX 文档: {path_argument}")
        if not resolved_path.is_file():
            raise FileNotFoundError(f"源文档不存在: {path_argument}")
        return resolved_path

    def _resolve_output_path(
        self,
        path_argument: Any,
        *,
        default_path: Path,
    ) -> Path:
        """解析并校验输出 DOCX 路径和父目录。

        Args:
            path_argument: 可选的相对或绝对输出路径。
            default_path: 未提供输出路径时使用的源文档路径。

        Returns:
            位于允许根目录内的 DOCX 输出路径。

        Raises:
            DocumentEditError: 输出参数、扩展名或父目录无效。
            PermissionError: 输出路径超出允许根目录。
        """
        if path_argument is None:
            return default_path
        if not isinstance(path_argument, str) or not path_argument.strip():
            raise DocumentEditError("output_path 必须是非空字符串")
        resolved_path = self._resolve_within_root(path_argument)
        if resolved_path.suffix.lower() != ".docx":
            raise DocumentEditError(f"输出路径必须使用 .docx 扩展名: {path_argument}")
        if not resolved_path.parent.is_dir():
            raise DocumentEditError(f"输出目录不存在: {resolved_path.parent}")
        if resolved_path.exists() and not resolved_path.is_file():
            raise DocumentEditError(f"输出路径不是文件: {resolved_path}")
        return resolved_path

    def _resolve_within_root(self, path_argument: str) -> Path:
        """解析路径并确保其位于工具允许访问的根目录内。

        Args:
            path_argument: 相对根目录的路径或根目录内的绝对路径。

        Returns:
            规范化后的绝对路径。

        Raises:
            PermissionError: 路径解析后超出允许根目录。
        """
        requested_path = Path(path_argument)
        candidate = (
            requested_path
            if requested_path.is_absolute()
            else self.root_directory / requested_path
        )
        resolved_path = candidate.resolve()
        try:
            resolved_path.relative_to(self.root_directory)
        except ValueError as error:
            raise PermissionError(
                f"禁止访问工作目录外的文档: {path_argument}"
            ) from error
        return resolved_path

    @staticmethod
    def _create_temporary_path(output_path: Path) -> Path:
        """在输出目录中创建供原子替换使用的唯一临时文件。

        Args:
            output_path: 最终输出 DOCX 路径。

        Returns:
            已创建且已关闭的临时文件路径。
        """
        with NamedTemporaryFile(
            prefix=f".{output_path.stem}-",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary_file:
            return Path(temporary_file.name)

    @staticmethod
    def _calculate_revision(path: Path) -> str:
        """计算 DOCX 二进制内容的 SHA-256 版本标识。

        Args:
            path: 需要计算版本的 DOCX 路径。

        Returns:
            64 位十六进制 SHA-256 字符串。
        """
        digest = hashlib.sha256()
        with path.open("rb") as document_file:
            for chunk in iter(lambda: document_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
