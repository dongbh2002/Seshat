"""解析 DOCX 的 OOXML 部件并生成结构化内容块。"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from zipfile import ZipFile

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from backend.tools.read_document.models import _DocumentBlock

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_2010_NAMESPACE = "http://schemas.microsoft.com/office/word/2010/wordml"
_DRAWING_NAMESPACE = (
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
)
_MATH_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_NAMESPACES = {
    "w": _WORD_NAMESPACE,
    "w14": _WORD_2010_NAMESPACE,
    "wp": _DRAWING_NAMESPACE,
    "m": _MATH_NAMESPACE,
}


def _qualified_name(tag: str) -> str:
    """将带命名空间前缀的标签转换为 lxml 使用的完整名称。

    Args:
        tag: 形如 ``w:p`` 或 ``w14:paraId`` 的 XML 标签或属性名。

    Returns:
        形如 ``{namespace}p`` 的完整 XML 名称。
    """
    prefix, local_name = tag.split(":", maxsplit=1)
    return f"{{{_NAMESPACES[prefix]}}}{local_name}"


class _DocxParser:
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
        for style in root.iter(_qualified_name("w:style")):
            style_id = style.get(_qualified_name("w:styleId")) or ""
            name_element = style.find("w:name", _NAMESPACES)
            style_name = (
                name_element.get(_qualified_name("w:val"))
                if name_element is not None
                else style_id
            )
            outline_element = style.find("w:pPr/w:outlineLvl", _NAMESPACES)
            heading_level = (
                int(outline_element.get(_qualified_name("w:val"), "0")) + 1
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
            numbering = style.find("w:pPr/w:numPr", _NAMESPACES)
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
        for abstract in root.iter(_qualified_name("w:abstractNum")):
            abstract_id = abstract.get(_qualified_name("w:abstractNumId")) or ""
            levels: dict[str, str] = {}
            for level in abstract.iter(_qualified_name("w:lvl")):
                level_id = level.get(_qualified_name("w:ilvl")) or "0"
                format_element = level.find("w:numFmt", _NAMESPACES)
                levels[level_id] = (
                    format_element.get(_qualified_name("w:val"), "bullet")
                    if format_element is not None
                    else "bullet"
                )
            abstract_numbering[abstract_id] = levels

        numbering: dict[str, dict[str, str]] = {}
        for number in root.iter(_qualified_name("w:num")):
            number_id = number.get(_qualified_name("w:numId")) or ""
            abstract_reference = number.find("w:abstractNumId", _NAMESPACES)
            if abstract_reference is not None:
                abstract_id = abstract_reference.get(_qualified_name("w:val"), "")
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
        for comment in root.iter(_qualified_name("w:comment")):
            comment_id = comment.get(_qualified_name("w:id")) or ""
            comments[comment_id] = {
                "id": comment_id,
                "author": comment.get(_qualified_name("w:author")) or "",
                "text": "".join(
                    text.text or "" for text in comment.iter(_qualified_name("w:t"))
                ),
            }
        return comments

    def _extract_text(self, node: Any, mode: str) -> str:
        """提取 XML 节点文本，并按指定模式呈现修订和特殊元素。

        Args:
            node: 需要提取文本的 OOXML 节点。
            mode: ``accepted`` 或 ``markup`` 修订视图。

        Returns:
            合并后的文本、修订标记和图片或注释引用占位符。
        """
        parts: list[str] = []
        for element in node.iter():
            ancestor_names = {
                etree.QName(ancestor).localname for ancestor in element.iterancestors()
            }
            in_deletion = "del" in ancestor_names
            in_insertion = "ins" in ancestor_names

            if element.tag == _qualified_name("w:t"):
                text = element.text or ""
                if in_deletion:
                    if mode == "markup":
                        parts.append(f"[-{text}-]")
                elif in_insertion and mode == "markup":
                    parts.append(f"{{+{text}+}}")
                else:
                    parts.append(text)
            elif element.tag == _qualified_name("w:delText") and mode == "markup":
                parts.append(f"[-{element.text or ''}-]")
            elif element.tag == _qualified_name("m:t"):
                parts.append(element.text or "")
            elif element.tag == _qualified_name("w:tab"):
                parts.append("\t")
            elif element.tag in {
                _qualified_name("w:br"),
                _qualified_name("w:cr"),
            }:
                parts.append("\n")
            elif element.tag == _qualified_name("w:drawing") and not in_deletion:
                drawing_properties = element.find(".//wp:docPr", _NAMESPACES)
                description = ""
                if drawing_properties is not None:
                    description = (
                        drawing_properties.get("descr")
                        or drawing_properties.get("name")
                        or ""
                    )
                parts.append(f"[图片：{description}]" if description else "[图片]")
            elif element.tag == _qualified_name("w:pict") and not in_deletion:
                parts.append("[图片]")
            elif element.tag == _qualified_name("w:footnoteReference"):
                parts.append(f"[脚注:{element.get(_qualified_name('w:id'), '')}]")
            elif element.tag == _qualified_name("w:endnoteReference"):
                parts.append(f"[尾注:{element.get(_qualified_name('w:id'), '')}]")

        return "".join(parts).replace("+}{+", "").replace("-][-", "")

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
    ) -> _DocumentBlock | None:
        """将一个 Word 段落解析为可定位的内容块。

        Args:
            paragraph: ``w:p`` 段落节点。
            xml_index: 段落在正文 body 中的位置。
            mode: 修订内容的展示模式。

        Returns:
            解析后的段落块；无内容的普通空段落返回 None。
        """
        text = self._extract_text(paragraph, mode).strip()
        has_changes = (
            paragraph.find(".//w:ins", _NAMESPACES) is not None
            or paragraph.find(".//w:del", _NAMESPACES) is not None
        )
        comment_ids = list(
            dict.fromkeys(
                marker.get(_qualified_name("w:id"), "")
                for marker in paragraph.iter()
                if marker.tag
                in {
                    _qualified_name("w:commentRangeStart"),
                    _qualified_name("w:commentReference"),
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

        style_element = paragraph.find("w:pPr/w:pStyle", _NAMESPACES)
        style_id = (
            style_element.get(_qualified_name("w:val"), "")
            if style_element is not None
            else ""
        )
        style_name, heading_level = self.styles.get(style_id, (style_id, None))
        outline_element = paragraph.find("w:pPr/w:outlineLvl", _NAMESPACES)
        if outline_element is not None:
            heading_level = int(outline_element.get(_qualified_name("w:val"), "0")) + 1

        numbering = paragraph.find("w:pPr/w:numPr", _NAMESPACES)
        if numbering is None:
            numbering = self._style_numbering.get(style_id)

        kind = "paragraph"
        level = 0
        if heading_level is not None and 1 <= heading_level <= 9:
            kind = "heading"
            level = heading_level
        elif numbering is not None:
            level_element = numbering.find("w:ilvl", _NAMESPACES)
            number_element = numbering.find("w:numId", _NAMESPACES)
            level = (
                int(level_element.get(_qualified_name("w:val"), "0"))
                if level_element is not None
                else 0
            )
            number_id = (
                number_element.get(_qualified_name("w:val"), "")
                if number_element is not None
                else ""
            )
            number_format = self.numbering.get(number_id, {}).get(str(level), "bullet")
            kind = "list_item"
            style_name = (
                "ordered" if number_format not in {"bullet", "none"} else "bullet"
            )

        para_id = paragraph.get(_qualified_name("w14:paraId")) or ""
        preferred_id = f"p-{para_id.lower()}" if para_id else f"p-{xml_index}"
        block_id = self._claim_block_id(preferred_id, f"p-{xml_index}")
        return _DocumentBlock(
            block_id=block_id,
            kind=kind,
            text=text,
            level=level,
            style=style_name or "",
            para_id=para_id,
            xml_index=xml_index,
            has_changes=has_changes,
            comments=comments,
        )

    def _parse_table(
        self,
        table: Any,
        xml_index: int,
        mode: str,
    ) -> _DocumentBlock | None:
        """将 Word 表格解析为保持合并关系提示的 Markdown 表格。

        Args:
            table: ``w:tbl`` 表格节点。
            xml_index: 表格在正文 body 中的位置。
            mode: 修订内容的展示模式。

        Returns:
            解析后的表格块；空表格返回 None。
        """
        rows: list[list[str]] = []
        for table_row in table.findall("w:tr", _NAMESPACES):
            row: list[str] = []
            for table_cell in table_row.findall("w:tc", _NAMESPACES):
                span_element = table_cell.find("w:tcPr/w:gridSpan", _NAMESPACES)
                column_span = (
                    int(span_element.get(_qualified_name("w:val"), "1"))
                    if span_element is not None
                    else 1
                )
                vertical_merge = table_cell.find("w:tcPr/w:vMerge", _NAMESPACES)
                cell_text = " <br> ".join(
                    filter(
                        None,
                        (
                            self._extract_text(paragraph, mode).strip()
                            for paragraph in table_cell.findall(".//w:p", _NAMESPACES)
                        ),
                    )
                )
                if (
                    vertical_merge is not None
                    and vertical_merge.get(_qualified_name("w:val")) != "restart"
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
            table.find(".//w:ins", _NAMESPACES) is not None
            or table.find(".//w:del", _NAMESPACES) is not None
        )
        return _DocumentBlock(
            block_id=self._claim_block_id(f"t-{xml_index}", f"t-{xml_index}"),
            kind="table",
            text="\n".join(markdown_lines),
            xml_index=xml_index,
            has_changes=has_changes,
        )

    def parse_blocks(self, mode: str) -> list[_DocumentBlock]:
        """按正文原始顺序解析段落、表格和内容控件。

        Args:
            mode: 修订内容的展示模式。

        Returns:
            按 document.xml 中出现顺序排列的内容块。

        Raises:
            ValueError: 正文 XML 缺少 body 节点。
        """
        body = self.document.find("w:body", _NAMESPACES)
        if body is None:
            raise ValueError("DOCX 正文缺少 body 节点")

        blocks: list[_DocumentBlock] = []
        for xml_index, element in enumerate(body):
            local_name = etree.QName(element).localname
            block: _DocumentBlock | None = None
            if local_name == "p":
                block = self._parse_paragraph(element, xml_index, mode)
            elif local_name == "tbl":
                block = self._parse_table(element, xml_index, mode)
            elif local_name == "sdt":
                text = self._extract_text(element, mode).strip()
                if text:
                    block = _DocumentBlock(
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
                    for paragraph in root.iter(_qualified_name("w:p"))
                ]
                text = "\n".join(filter(None, texts))
                if text:
                    key = f"{header_or_footer.group(1)}s"
                    extras.setdefault(key, []).append({"part": part_name, "text": text})
                continue

            if note_part is None:
                continue
            note_name = "footnote" if note_part.group(1) == "footnotes" else "endnote"
            for note in root.iter(_qualified_name(f"w:{note_name}")):
                note_id = note.get(_qualified_name("w:id"), "-1")
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
        if self.document.find(".//w:altChunk", _NAMESPACES) is not None:
            warnings.append("外部嵌入内容 altChunk 尚未解析")
        if (
            self.document.find(".//w:moveFrom", _NAMESPACES) is not None
            or self.document.find(".//w:moveTo", _NAMESPACES) is not None
        ):
            warnings.append("移动修订尚未单独标记")
        if self.document.find(".//m:oMath", _NAMESPACES) is not None:
            warnings.append("公式当前只提取可见文本，尚未保留完整数学结构")
        # TODO: 后续补充文本框、图表、嵌入对象和关系引用的结构化索引。
        return warnings

