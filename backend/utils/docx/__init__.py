"""DOCX 底层能力：OOXML 解析、内容块、块 ID 修改、结构索引、版本间块对齐与路径版本校验。"""

from backend.utils.docx.diff import BlockPair, align_blocks
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
    build_heading_paths,
)
from backend.utils.docx.models import DocumentBlock
from backend.utils.docx.parser import (
    NAMESPACES,
    TEXT_MODES,
    DocxParser,
    is_position_based_block_id,
    qualified_name,
)

__all__ = [
    "NAMESPACES",
    "TEXT_MODES",
    "BlockPair",
    "DocumentBlock",
    "DocumentEditError",
    "DocumentHeading",
    "DocumentIndex",
    "DocumentSection",
    "DocumentSnapshot",
    "DocxEditor",
    "DocxParser",
    "align_blocks",
    "build_heading_paths",
    "calculate_revision",
    "is_position_based_block_id",
    "qualified_name",
    "resolve_docx_path",
    "resolve_within_root",
]
