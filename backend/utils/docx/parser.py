"""解析 DOCX 的 OOXML 部件并生成结构化内容块。"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any, ClassVar
from zipfile import ZipFile

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from backend.utils.docx.models import DocumentBlock

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_2010_NAMESPACE = "http://schemas.microsoft.com/office/word/2010/wordml"
_DRAWING_NAMESPACE = (
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
)
_MATH_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_STABLE_BLOCK_ID_PATTERN = re.compile(r"p-[0-9a-f]{8}")  # 由 w14:paraId 生成的块 ID。
NAMESPACES = {
    "w": _WORD_NAMESPACE,
    "w14": _WORD_2010_NAMESPACE,
    "wp": _DRAWING_NAMESPACE,
    "m": _MATH_NAMESPACE,
}
TEXT_MODES = ("accepted", "rejected", "markup")  # 修订视图：接受、拒绝、标记。


def qualified_name(tag: str) -> str:
    """将带命名空间前缀的标签转换为 lxml 使用的完整名称。

    Args:
        tag: 形如 ``w:p`` 或 ``w14:paraId`` 的 XML 标签或属性名。

    Returns:
        形如 ``{namespace}p`` 的完整 XML 名称。
    """
    prefix, local_name = tag.split(":", maxsplit=1)
    return f"{{{NAMESPACES[prefix]}}}{local_name}"


_REVISION_NAMES = frozenset({"ins", "del"})  # 包裹内容的修订节点本地名称。
_TEXT_TAGS = frozenset(  # 直接输出元素文本的标签。
    qualified_name(tag) for tag in ("w:t", "w:delText", "m:t")
)
_BREAK_TAGS = frozenset(qualified_name(tag) for tag in ("w:br", "w:cr"))  # 换行标签。
_DRAWING_TAGS = frozenset(  # 图片标签。
    qualified_name(tag) for tag in ("w:drawing", "w:pict")
)
_CONTENT_TAGS = (  # 提取文本时需要处理的全部内容标签。
    _TEXT_TAGS
    | _BREAK_TAGS
    | _DRAWING_TAGS
    | {
        qualified_name("w:tab"),
        qualified_name("w:footnoteReference"),
        qualified_name("w:endnoteReference"),
    }
)


def is_position_based_block_id(block_id: str) -> bool:
    """判断块 ID 是否按正文位置生成，插入或删除块后可能指向别的块。

    段落优先使用 8 位十六进制的 w14:paraId 生成 ``p-xxxxxxxx``；缺少 paraId 的
    段落（``p-<位置>``）、表格（``t-<位置>``）和内容控件（``content-<位置>``）
    都按位置生成。

    Args:
        block_id: 内容块 ID。

    Returns:
        按位置生成时为 True。
    """
    return _STABLE_BLOCK_ID_PATTERN.fullmatch(block_id) is None


class DocxParser:
    """从 DOCX 的 OOXML 部件中提取正文结构和附加内容。"""

    max_xml_bytes: ClassVar[int] = 50 * 1024 * 1024  # 单个 XML 部件的读取上限。

    def __init__(self, archive: ZipFile) -> None:
        """初始化 OOXML 解析器并加载读取所需的基础部件。

        Args:
            archive: 已打开的 DOCX ZIP 包。

        Returns:
            None。
        """
        self.archive = archive  # 当前读取的 DOCX ZIP 包。
        document = self._load_xml("word/document.xml", required=True)
        if document is None:
            raise ValueError("DOCX 缺少必需部件: word/document.xml")
        self.document = document  # 文档的非空正文 XML 根节点。
        self._style_numbering: dict[str, Any] = {}  # 样式内定义的列表编号属性。
        self.styles = self._load_styles()  # 样式 ID 到显示名和标题级别的映射。
        self.numbering = self._load_numbering()  # 编号 ID 到各层级格式的映射。
        self.comments = self._load_comments()  # 批注 ID 到批注内容的映射。
        self._used_ids: set[str] = set()  # 本次解析已经分配的内容块 ID。

    def _load_xml(self, part_name: str, *, required: bool = False) -> Any | None:
        """安全读取并解析一个 DOCX XML 部件。

        Args:
            part_name: ZIP 包内的 XML 部件路径。
            required: 部件缺失时是否应抛出异常。

        Returns:
            lxml XML 根元素；可选部件不存在时返回 None。

        Raises:
            ValueError: 必需部件缺失或 XML 部件超过大小限制。
        """
        try:
            part_info = self.archive.getinfo(part_name)
        except KeyError:
            if required:
                raise ValueError(f"DOCX 缺少必需部件: {part_name}") from None
            return None

        if part_info.file_size > self.max_xml_bytes:
            raise ValueError(f"DOCX XML 部件过大: {part_name}")

        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
            recover=False,
        )
        return etree.fromstring(self.archive.read(part_name), parser=parser)

    def _load_styles(self) -> dict[str, tuple[str, int | None]]:
        """读取段落样式的显示名称、标题层级和默认编号属性。

        Returns:
            样式 ID 到 ``(显示名, 标题层级)`` 的映射。
        """
        root = self._load_xml("word/styles.xml")
        if root is None:
            return {}

        styles: dict[str, tuple[str, int | None]] = {}
        for style in root.iter(qualified_name("w:style")):
            style_id = style.get(qualified_name("w:styleId")) or ""
            name_element = style.find("w:name", NAMESPACES)
            style_name = (
                name_element.get(qualified_name("w:val"))
                if name_element is not None
                else style_id
            )
            outline_element = style.find("w:pPr/w:outlineLvl", NAMESPACES)
            heading_level = (
                int(outline_element.get(qualified_name("w:val"), "0")) + 1
                if outline_element is not None
                else None
            )
            if heading_level is None:
                match = re.match(
                    r"(?:heading|标题)\s*(\d)",
                    style_name or "",
                    flags=re.IGNORECASE,
                )
                heading_level = int(match.group(1)) if match else None

            styles[style_id] = (style_name or style_id, heading_level)
            numbering = style.find("w:pPr/w:numPr", NAMESPACES)
            if numbering is not None:
                self._style_numbering[style_id] = numbering
        return styles

    def _load_numbering(self) -> dict[str, dict[str, str]]:
        """读取列表编号格式，用于区分有序和无序列表。

        Returns:
            编号 ID 到各缩进层级编号格式的映射。
        """
        root = self._load_xml("word/numbering.xml")
        if root is None:
            return {}

        abstract_numbering: dict[str, dict[str, str]] = {}
        for abstract in root.iter(qualified_name("w:abstractNum")):
            abstract_id = abstract.get(qualified_name("w:abstractNumId")) or ""
            levels: dict[str, str] = {}
            for level in abstract.iter(qualified_name("w:lvl")):
                level_id = level.get(qualified_name("w:ilvl")) or "0"
                format_element = level.find("w:numFmt", NAMESPACES)
                levels[level_id] = (
                    format_element.get(qualified_name("w:val"), "bullet")
                    if format_element is not None
                    else "bullet"
                )
            abstract_numbering[abstract_id] = levels

        numbering: dict[str, dict[str, str]] = {}
        for number in root.iter(qualified_name("w:num")):
            number_id = number.get(qualified_name("w:numId")) or ""
            abstract_reference = number.find("w:abstractNumId", NAMESPACES)
            if abstract_reference is not None:
                abstract_id = abstract_reference.get(qualified_name("w:val"), "")
                numbering[number_id] = abstract_numbering.get(abstract_id, {})
        return numbering

    def _load_comments(self) -> dict[str, dict[str, str]]:
        """读取文档批注的作者和文本。

        Returns:
            批注 ID 到批注结构的映射。
        """
        root = self._load_xml("word/comments.xml")
        if root is None:
            return {}

        comments: dict[str, dict[str, str]] = {}
        for comment in root.iter(qualified_name("w:comment")):
            comment_id = comment.get(qualified_name("w:id")) or ""
            comments[comment_id] = {
                "id": comment_id,
                "author": comment.get(qualified_name("w:author")) or "",
                "date": comment.get(qualified_name("w:date")) or "",
                "text": "".join(
                    text.text or "" for text in comment.iter(qualified_name("w:t"))
                ),
            }
        return comments

    def _extract_text(
        self,
        node: Any,
        mode: str,
        accepted_authors: Collection[str] | None = None,
    ) -> str:
        """提取 XML 节点文本，并按指定模式呈现修订和特殊元素。

        Args:
            node: 需要提取文本的 OOXML 节点。
            mode: 修订视图，取值见 ``parse_blocks``。
            accepted_authors: 只接受这些作者的修订，取值见 ``parse_blocks``。

        Returns:
            合并后的文本、修订标记和图片或注释引用占位符。
        """
        parts: list[str] = []
        for element in node.iter():
            if element.tag not in _CONTENT_TAGS:
                continue
            revisions = [
                (
                    etree.QName(ancestor).localname,
                    ancestor.get(qualified_name("w:author")) or "",
                )
                for ancestor in element.iterancestors()
                if etree.QName(ancestor).localname in _REVISION_NAMES
            ]
            if mode == "markup":
                in_deletion = any(kind == "del" for kind, _ in revisions)
                in_insertion = any(kind == "ins" for kind, _ in revisions)
                if element.tag == qualified_name("w:delText") or (
                    element.tag == qualified_name("w:t") and in_deletion
                ):
                    parts.append(f"[-{element.text or ''}-]")
                    continue
                if element.tag == qualified_name("w:t") and in_insertion:
                    parts.append(f"{{+{element.text or ''}+}}")
                    continue
                if element.tag in _DRAWING_TAGS and in_deletion:
                    continue
            elif self._is_hidden(revisions, mode, accepted_authors):
                continue
            parts.append(self._render_content(element))

        return "".join(parts).replace("+}{+", "").replace("-][-", "")

    @staticmethod
    def _is_hidden(
        revisions: list[tuple[str, str]],
        mode: str,
        accepted_authors: Collection[str] | None,
    ) -> bool:
        """判断修订内的内容在 accepted / rejected 视图中是否不可见。

        内容可见当且仅当外层插入修订全部被接受、删除修订全部未被接受。

        Args:
            revisions: 内容外层的修订，每项为 (``ins`` 或 ``del``, 作者)。
            mode: ``accepted`` 或 ``rejected``。
            accepted_authors: 只接受这些作者的修订；None 时按 mode 全部接受或全部拒绝。

        Returns:
            不可见时为 True。
        """
        for kind, author in revisions:
            accepted = (
                author in accepted_authors
                if accepted_authors is not None
                else mode == "accepted"
            )
            if (kind == "del" and accepted) or (kind == "ins" and not accepted):
                return True
        return False

    @staticmethod
    def _render_content(element: Any) -> str:
        """把一个内容元素渲染为文本或占位符。

        Args:
            element: 标签属于 ``_CONTENT_TAGS`` 的元素。

        Returns:
            文本、制表符、换行，或图片、脚注、尾注占位符。
        """
        tag = element.tag
        if tag in _TEXT_TAGS:
            return element.text or ""
        if tag == qualified_name("w:tab"):
            return "\t"
        if tag in _BREAK_TAGS:
            return "\n"
        if tag == qualified_name("w:drawing"):
            drawing_properties = element.find(".//wp:docPr", NAMESPACES)
            description = ""
            if drawing_properties is not None:
                description = (
                    drawing_properties.get("descr")
                    or drawing_properties.get("name")
                    or ""
                )
            return f"[图片：{description}]" if description else "[图片]"
        if tag == qualified_name("w:pict"):
            return "[图片]"
        if tag == qualified_name("w:footnoteReference"):
            return f"[脚注:{element.get(qualified_name('w:id'), '')}]"
        return f"[尾注:{element.get(qualified_name('w:id'), '')}]"

    @staticmethod
    def _collect_revisions(node: Any) -> list[dict[str, str]]:
        """收集节点内插入与删除修订的作者，每位作者一条，时间取最晚。

        Args:
            node: 段落或表格节点。

        Returns:
            按作者首次出现顺序排列的 ``{"author", "date"}`` 列表。
        """
        latest: dict[str, str] = {}
        for revision in node.iter(qualified_name("w:ins"), qualified_name("w:del")):
            author = revision.get(qualified_name("w:author")) or ""
            date = revision.get(qualified_name("w:date")) or ""
            latest[author] = max(latest.get(author, ""), date)
        return [{"author": author, "date": date} for author, date in latest.items()]

    def _claim_block_id(self, preferred_id: str, fallback_id: str) -> str:
        """分配当前解析结果中唯一的内容块 ID。

        Args:
            preferred_id: 优先使用的 Word 原生稳定 ID。
            fallback_id: 原生 ID 缺失或重复时使用的位置 ID。

        Returns:
            当前解析结果中唯一的内容块 ID。
        """
        block_id = preferred_id if preferred_id not in self._used_ids else fallback_id
        self._used_ids.add(block_id)
        return block_id

    def _parse_paragraph(
        self,
        paragraph: Any,
        xml_index: int,
        mode: str,
        accepted_authors: Collection[str] | None,
    ) -> DocumentBlock | None:
        """将一个 Word 段落解析为可定位的内容块。

        Args:
            paragraph: ``w:p`` 段落节点。
            xml_index: 段落在正文 body 中的位置。
            mode: 修订内容的展示模式。
            accepted_authors: 只接受这些作者的修订，取值见 ``parse_blocks``。

        Returns:
            解析后的段落块；无内容的普通空段落返回 None。
        """
        text = self._extract_text(paragraph, mode, accepted_authors).strip()
        has_changes = (
            paragraph.find(".//w:ins", NAMESPACES) is not None
            or paragraph.find(".//w:del", NAMESPACES) is not None
        )
        comment_ids = list(
            dict.fromkeys(
                marker.get(qualified_name("w:id"), "")
                for marker in paragraph.iter()
                if marker.tag
                in {
                    qualified_name("w:commentRangeStart"),
                    qualified_name("w:commentReference"),
                }
            )
        )
        comments = [
            self.comments[comment_id]
            for comment_id in comment_ids
            if comment_id in self.comments
        ]
        if not text and not has_changes and not comments:
            return None

        style_element = paragraph.find("w:pPr/w:pStyle", NAMESPACES)
        style_id = (
            style_element.get(qualified_name("w:val"), "")
            if style_element is not None
            else ""
        )
        style_name, heading_level = self.styles.get(style_id, (style_id, None))
        outline_element = paragraph.find("w:pPr/w:outlineLvl", NAMESPACES)
        if outline_element is not None:
            heading_level = int(outline_element.get(qualified_name("w:val"), "0")) + 1

        numbering = paragraph.find("w:pPr/w:numPr", NAMESPACES)
        if numbering is None:
            numbering = self._style_numbering.get(style_id)

        kind = "paragraph"
        level = 0
        if heading_level is not None and 1 <= heading_level <= 9:
            kind = "heading"
            level = heading_level
        elif numbering is not None:
            level_element = numbering.find("w:ilvl", NAMESPACES)
            number_element = numbering.find("w:numId", NAMESPACES)
            level = (
                int(level_element.get(qualified_name("w:val"), "0"))
                if level_element is not None
                else 0
            )
            number_id = (
                number_element.get(qualified_name("w:val"), "")
                if number_element is not None
                else ""
            )
            number_format = self.numbering.get(number_id, {}).get(str(level), "bullet")
            kind = "list_item"
            style_name = (
                "ordered" if number_format not in {"bullet", "none"} else "bullet"
            )

        para_id = paragraph.get(qualified_name("w14:paraId")) or ""
        preferred_id = f"p-{para_id.lower()}" if para_id else f"p-{xml_index}"
        block_id = self._claim_block_id(preferred_id, f"p-{xml_index}")
        return DocumentBlock(
            block_id=block_id,
            kind=kind,
            text=text,
            level=level,
            style=style_name or "",
            para_id=para_id,
            xml_index=xml_index,
            has_changes=has_changes,
            revisions=self._collect_revisions(paragraph),
            comments=comments,
        )

    def _parse_table(
        self,
        table: Any,
        xml_index: int,
        mode: str,
        accepted_authors: Collection[str] | None,
    ) -> DocumentBlock | None:
        """将 Word 表格解析为保持合并关系提示的 Markdown 表格。

        Args:
            table: ``w:tbl`` 表格节点。
            xml_index: 表格在正文 body 中的位置。
            mode: 修订内容的展示模式。
            accepted_authors: 只接受这些作者的修订，取值见 ``parse_blocks``。

        Returns:
            解析后的表格块；空表格返回 None。
        """
        rows: list[list[str]] = []
        for table_row in table.findall("w:tr", NAMESPACES):
            row: list[str] = []
            for table_cell in table_row.findall("w:tc", NAMESPACES):
                span_element = table_cell.find("w:tcPr/w:gridSpan", NAMESPACES)
                column_span = (
                    int(span_element.get(qualified_name("w:val"), "1"))
                    if span_element is not None
                    else 1
                )
                vertical_merge = table_cell.find("w:tcPr/w:vMerge", NAMESPACES)
                cell_text = " <br> ".join(
                    filter(
                        None,
                        (
                            self._extract_text(
                                paragraph, mode, accepted_authors
                            ).strip()
                            for paragraph in table_cell.findall(".//w:p", NAMESPACES)
                        ),
                    )
                )
                if (
                    vertical_merge is not None
                    and vertical_merge.get(qualified_name("w:val")) != "restart"
                ):
                    cell_text = "〃"
                row.append(cell_text.replace("|", "\\|"))
                row.extend([""] * (column_span - 1))
            rows.append(row)

        if not rows:
            return None
        width = max(len(row) for row in rows)
        if width == 0:
            return None
        normalized_rows = [row + [""] * (width - len(row)) for row in rows]
        markdown_lines = [
            "| " + " | ".join(normalized_rows[0]) + " |",
            "|" + "---|" * width,
        ]
        markdown_lines.extend(
            "| " + " | ".join(row) + " |" for row in normalized_rows[1:]
        )
        has_changes = (
            table.find(".//w:ins", NAMESPACES) is not None
            or table.find(".//w:del", NAMESPACES) is not None
        )
        return DocumentBlock(
            block_id=self._claim_block_id(f"t-{xml_index}", f"t-{xml_index}"),
            kind="table",
            text="\n".join(markdown_lines),
            xml_index=xml_index,
            has_changes=has_changes,
            revisions=self._collect_revisions(table),
        )

    def parse_blocks(
        self,
        mode: str,
        *,
        accepted_authors: Collection[str] | None = None,
    ) -> list[DocumentBlock]:
        """按正文原始顺序解析段落、表格和内容控件。

        同一解析器只应调用一次：块 ID 按本次解析分配，重复调用会被判为冲突。

        Args:
            mode: 修订视图，取值见 ``TEXT_MODES``：``accepted`` 接受全部修订，
                ``rejected`` 拒绝全部修订（修改前原文），``markup`` 显式标记增删。
            accepted_authors: 仅 accepted 视图可用，只接受这些作者的修订、
                拒绝其余作者的修订；None 表示接受全部。

        Returns:
            按 document.xml 中出现顺序排列的内容块；同一文档各视图的块 ID 一致。

        Raises:
            ValueError: 视图无效，或正文 XML 缺少 body 节点。
        """
        if mode not in TEXT_MODES:
            raise ValueError(f"修订视图无效: {mode}")
        if accepted_authors is not None and mode != "accepted":
            raise ValueError("accepted_authors 只能用于 accepted 视图")
        body = self.document.find("w:body", NAMESPACES)
        if body is None:
            raise ValueError("DOCX 正文缺少 body 节点")

        blocks: list[DocumentBlock] = []
        for xml_index, element in enumerate(body):
            local_name = etree.QName(element).localname
            block: DocumentBlock | None = None
            if local_name == "p":
                block = self._parse_paragraph(
                    element, xml_index, mode, accepted_authors
                )
            elif local_name == "tbl":
                block = self._parse_table(element, xml_index, mode, accepted_authors)
            elif local_name == "sdt":
                text = self._extract_text(element, mode, accepted_authors).strip()
                if text:
                    block = DocumentBlock(
                        block_id=self._claim_block_id(
                            f"content-{xml_index}", f"content-{xml_index}"
                        ),
                        kind="paragraph",
                        text=text,
                        style="content-control",
                        xml_index=xml_index,
                    )
            if block is not None:
                blocks.append(block)
        return blocks

    def read_extras(self, mode: str) -> dict[str, list[dict[str, str]]]:
        """读取正文之外的页眉、页脚、脚注和尾注文本。

        Args:
            mode: 修订内容的展示模式。

        Returns:
            按附加部件类型分组的文本与来源信息。
        """
        extras: dict[str, list[dict[str, str]]] = {}
        for part_name in self.archive.namelist():
            header_or_footer = re.fullmatch(r"word/(header|footer)\d*\.xml", part_name)
            note_part = re.fullmatch(r"word/(footnotes|endnotes)\.xml", part_name)
            if not header_or_footer and not note_part:
                continue

            root = self._load_xml(part_name)
            if root is None:
                continue
            if header_or_footer is not None:
                texts = [
                    self._extract_text(paragraph, mode).strip()
                    for paragraph in root.iter(qualified_name("w:p"))
                ]
                text = "\n".join(filter(None, texts))
                if text:
                    key = f"{header_or_footer.group(1)}s"
                    extras.setdefault(key, []).append({"part": part_name, "text": text})
                continue

            if note_part is None:
                continue
            note_name = "footnote" if note_part.group(1) == "footnotes" else "endnote"
            for note in root.iter(qualified_name(f"w:{note_name}")):
                note_id = note.get(qualified_name("w:id"), "-1")
                if int(note_id) < 0:
                    continue
                text = self._extract_text(note, mode).strip()
                if text:
                    extras.setdefault(note_part.group(1), []).append(
                        {"id": note_id, "part": part_name, "text": text}
                    )
        return extras

    def get_warnings(self) -> list[str]:
        """返回当前初版读取器对特殊内容的处理限制。

        Returns:
            需要模型和调用方注意的读取限制列表。
        """
        names = set(self.archive.namelist())
        warnings: list[str] = []
        if any(name.startswith("word/media/") for name in names):
            warnings.append("图片仅以位置占位符呈现，尚未识别图片视觉内容")
        if self.document.find(".//w:altChunk", NAMESPACES) is not None:
            warnings.append("外部嵌入内容 altChunk 尚未解析")
        if (
            self.document.find(".//w:moveFrom", NAMESPACES) is not None
            or self.document.find(".//w:moveTo", NAMESPACES) is not None
        ):
            warnings.append("移动修订尚未单独标记")
        if self.document.find(".//m:oMath", NAMESPACES) is not None:
            warnings.append("公式当前只提取可见文本，尚未保留完整数学结构")
        # TODO: 后续补充文本框、图表、嵌入对象和关系引用的结构化索引。
        return warnings
