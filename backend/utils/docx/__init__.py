"""DOCX 底层能力：OOXML 解析、内容块、块 ID 修改、结构索引与路径版本校验。"""

from backend.utils.docx.editor import DocumentEditError, DocxEditor
from backend.utils.docx.files import (
    calculate_revision,
    resolve_docx_path,
    resolve_within_root,
)
from backend.utils.docx.index import (
    DocumentHeading,
    DocumentIndex,
    DocumentSection,
    DocumentSnapshot,
)
from backend.utils.docx.models import DocumentBlock
from backend.utils.docx.parser import (
    NAMESPACES,
    DocxParser,
    is_position_based_block_id,
    qualified_name,
)

__all__ = [
    "NAMESPACES",
    "DocumentBlock",
    "DocumentEditError",
    "DocumentHeading",
    "DocumentIndex",
    "DocumentSection",
    "DocumentSnapshot",
    "DocxEditor",
    "DocxParser",
    "calculate_revision",
    "is_position_based_block_id",
    "qualified_name",
    "resolve_docx_path",
    "resolve_within_root",
]
