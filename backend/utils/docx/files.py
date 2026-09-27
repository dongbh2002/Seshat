"""DOCX 文件路径校验与版本计算，供读写工具和文档索引共用。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def resolve_within_root(root_directory: Path, path: str) -> Path:
    """解析路径并确保其位于允许根目录内，不要求文件已存在。

    Args:
        root_directory: 已解析的允许访问根目录。
        path: 相对路径或位于根目录内的绝对路径。

    Returns:
        规范化后的绝对路径。

    Raises:
        PermissionError: 解析后的路径超出允许根目录。
    """
    requested_path = Path(path)
    candidate = (
        requested_path
        if requested_path.is_absolute()
        else root_directory / requested_path
    )
    resolved_path = candidate.resolve()
    try:
        resolved_path.relative_to(root_directory)
    except ValueError as error:
        raise PermissionError(f"禁止访问工作目录外的文档: {path}") from error
    return resolved_path


def resolve_docx_path(root_directory: Path, path: str) -> Path:
    """解析并校验位于允许根目录内的 DOCX 路径。

    Args:
        root_directory: 已解析的允许访问根目录。
        path: 相对路径或位于根目录内的绝对路径。

    Returns:
        已解析且通过访问范围检查的 DOCX 文件路径。

    Raises:
        ValueError: 文件扩展名不是 DOCX。
        PermissionError: 解析后的路径超出允许根目录。
        FileNotFoundError: 路径不存在或不是文件。
    """
    resolved_path = resolve_within_root(root_directory, path)
    if resolved_path.suffix.lower() != ".docx":
        raise ValueError(f"当前仅支持 DOCX 文档: {path}")
    if not resolved_path.is_file():
        raise FileNotFoundError(f"文档不存在: {path}")
    return resolved_path


def calculate_revision(path: Path) -> str:
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
