"""在原 DOCX OOXML 树上执行最小范围修改并保存修改后的文档。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from backend.tools.read_document.parser import (
    _NAMESPACES,
    _DocxParser,
    _qualified_name,
)

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_PACKAGE_RELATIONSHIPS_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_COMMENTS_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
_COMMENTS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
)
_COMMENTS_PART = "word/comments.xml"
_DOCUMENT_PART = "word/document.xml"
_DOCUMENT_RELATIONSHIPS_PART = "word/_rels/document.xml.rels"
_CONTENT_TYPES_PART = "[Content_Types].xml"


class DocumentEditError(ValueError):
    """表示模型可根据错误信息调整参数并重试的文档修改错误。"""


class DocxEditor:
    """基于读取工具生成的内容块 ID 修改一个 DOCX 文档。"""

    supported_operations: ClassVar[frozenset[str]] = frozenset(  # 首版支持的操作。
        {"replace_text", "insert_after", "delete", "set_cell", "comment"}
    )

    def __init__(self, source_path: Path, *, author: str, tracked: bool) -> None:
        """打开源 DOCX，并在修改前建立内容块 ID 到 XML 元素的固定映射。

        Args:
            source_path: 需要修改的 DOCX 文件路径。
            author: 修订和批注中记录的作者名称。
            tracked: 是否使用 Word 修订标记保存文本变化。

        Returns:
            None。

        Raises:
            DocumentEditError: 文档正文结构无效。
        """
        self.source_path = source_path  # 当前编辑器读取的源 DOCX 路径。
        self.author = author  # 修订和批注使用的作者名称。
        self.tracked = tracked  # 是否生成 w:ins 和 w:del 修订节点。
        self.timestamp = datetime.now(timezone.utc).strftime(  # 本批修改的 UTC 时间。
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.archive = ZipFile(source_path)  # 修改期间保持打开的源 DOCX ZIP 包。
        try:
            self.parser = _DocxParser(self.archive)  # 与读取工具共用的 ID 解析器。
            body = self.parser.document.find("w:body", _NAMESPACES)
            if body is None:
                raise DocumentEditError("DOCX 正文缺少 body 节点")
            blocks = self.parser.parse_blocks("accepted")
        except Exception:
            self.archive.close()
            raise

        self.document = self.parser.document  # 将被最小范围修改的 document.xml 树。
        body_children = list(body)
        self.elements = {  # 修改开始前固定的内容块 ID 到原 XML 元素映射。
            block.block_id: body_children[block.xml_index]
            for block in blocks
            if 0 <= block.xml_index < len(body_children)
        }
        self.extra_parts: dict[str, bytes] = {}  # 新增或修改的非正文 OOXML 部件。
        self._next_id = self._maximum_existing_id() + 1  # 下一可用修订或批注 ID。
        self._insert_tails: dict[str, Any] = {}  # 同一锚点连续插入时的末尾元素。

    def __enter__(self) -> DocxEditor:
        """进入编辑器上下文。

        Returns:
            当前编辑器实例。
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出编辑器上下文并关闭源 DOCX。

        Args:
            exc_type: 上下文内异常类型，没有异常时为 None。
            exc_value: 上下文内异常实例，没有异常时为 None。
            traceback: 上下文内异常调用栈，没有异常时为 None。

        Returns:
            None。
        """
        self.close()

    def close(self) -> None:
        """关闭源 DOCX ZIP 包。

        Returns:
            None。
        """
        self.archive.close()

    def apply(self, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """先校验整批操作和目标 ID，再按给定顺序执行修改。

        Args:
            operations: 使用内容块 ID 定位的修改操作列表。

        Returns:
            每项操作的名称、目标 ID 和可读结果。

        Raises:
            DocumentEditError: 操作参数、目标类型或目标状态不允许修改。
        """
        normalized_operations = self._validate_operations(operations)
        results: list[dict[str, Any]] = []
        for index, operation in enumerate(normalized_operations):
            operation_name = operation["op"]
            target_id = operation["id"]
            target = self.elements[target_id]
            arguments = {
                key: value
                for key, value in operation.items()
                if key not in {"op", "id"}
            }
            if operation_name == "replace_text":
                message = self._op_replace_text(target, **arguments)
            elif operation_name == "insert_after":
                message = self._op_insert_after(
                    target,
                    target_id=target_id,
                    **arguments,
                )
            elif operation_name == "delete":
                message = self._op_delete(target)
            elif operation_name == "set_cell":
                message = self._op_set_cell(target, **arguments)
            else:
                message = self._op_comment(target, **arguments)
            results.append(
                {
                    "index": index,
                    "op": operation_name,
                    "id": target_id,
                    "message": message,
                }
            )
        return results

    def save(self, output_path: Path) -> None:
        """保存 DOCX，仅序列化正文和本批修改涉及的附加部件。

        Args:
            output_path: 新 DOCX 的临时或最终输出路径。

        Returns:
            None。
        """
        existing_names = set(self.archive.namelist())
        with ZipFile(output_path, "w", allowZip64=True) as destination:
            destination.comment = self.archive.comment
            for item in self.archive.infolist():
                if item.filename == _DOCUMENT_PART:
                    data = self._serialize(self.document)
                elif item.filename in self.extra_parts:
                    data = self.extra_parts[item.filename]
                else:
                    data = self.archive.read(item.filename)
                destination.writestr(item, data)

            for part_name, data in self.extra_parts.items():
                if part_name not in existing_names:
                    destination.writestr(
                        part_name,
                        data,
                        compress_type=ZIP_DEFLATED,
                    )

    def _validate_operations(
        self,
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """校验整批操作的字段、目标 ID、目标类型和冲突。

        Args:
            operations: 尚未执行的原始操作列表。

        Returns:
            可安全执行的操作字典浅拷贝列表。

        Raises:
            DocumentEditError: 任一操作无效时抛出，不执行任何修改。
        """
        normalized: list[dict[str, Any]] = []
        operations_by_target: dict[str, list[str]] = {}
        for index, raw_operation in enumerate(operations):
            if not isinstance(raw_operation, dict):
                raise DocumentEditError(f"operations[{index}] 必须是对象")
            operation = dict(raw_operation)
            operation_name = operation.get("op")
            target_id = operation.get("id")
            if operation_name not in self.supported_operations:
                raise DocumentEditError(
                    f"operations[{index}] 不支持操作 {operation_name!r}"
                )
            if not isinstance(target_id, str) or not target_id.strip():
                raise DocumentEditError(f"operations[{index}].id 必须是非空字符串")
            if target_id not in self.elements:
                raise DocumentEditError(
                    f"未知 ID {target_id!r}，请重新读取文档获取最新 ID"
                )

            self._validate_operation_arguments(index, operation)
            self._validate_target(index, operation_name, self.elements[target_id])
            operations_by_target.setdefault(target_id, []).append(operation_name)
            normalized.append(operation)

        for target_id, operation_names in operations_by_target.items():
            if "delete" in operation_names and len(operation_names) > 1:
                raise DocumentEditError(
                    f"ID {target_id!r} 同一批次中不能同时删除并执行其他操作"
                )
        return normalized

    def _validate_operation_arguments(
        self,
        index: int,
        operation: dict[str, Any],
    ) -> None:
        """校验一项操作的必需字段、可选字段和基础数据类型。

        Args:
            index: 操作在批次中的零基位置。
            operation: 需要校验的操作字典。

        Returns:
            None。

        Raises:
            DocumentEditError: 字段缺失、字段多余或类型错误。
        """
        operation_name = operation["op"]
        field_rules: dict[str, tuple[set[str], set[str]]] = {
            "replace_text": ({"op", "id", "old", "new"}, {"occurrence"}),
            "insert_after": ({"op", "id", "text"}, {"style"}),
            "delete": ({"op", "id"}, set()),
            "set_cell": ({"op", "id", "row", "col", "text"}, set()),
            "comment": ({"op", "id", "text"}, {"quote", "occurrence"}),
        }
        required, optional = field_rules[operation_name]
        missing = required - operation.keys()
        unexpected = operation.keys() - required - optional
        if missing:
            raise DocumentEditError(
                f"operations[{index}] 缺少字段: {', '.join(sorted(missing))}"
            )
        if unexpected:
            raise DocumentEditError(
                f"operations[{index}] 包含不支持字段: {', '.join(sorted(unexpected))}"
            )

        if operation_name == "replace_text":
            self._require_text(index, operation, "old", allow_empty=False)
            self._require_text(index, operation, "new", allow_empty=True)
        elif operation_name == "insert_after":
            self._require_text(index, operation, "text", allow_empty=False)
            if "style" in operation:
                self._require_text(index, operation, "style", allow_empty=False)
        elif operation_name == "set_cell":
            self._require_non_negative_integer(index, operation, "row")
            self._require_non_negative_integer(index, operation, "col")
            self._require_text(index, operation, "text", allow_empty=True)
        elif operation_name == "comment":
            self._require_text(index, operation, "text", allow_empty=False)
            if "quote" in operation:
                self._require_text(index, operation, "quote", allow_empty=False)

        if "occurrence" in operation:
            occurrence = operation["occurrence"]
            if (
                not isinstance(occurrence, int)
                or isinstance(occurrence, bool)
                or occurrence < 1
            ):
                raise DocumentEditError(
                    f"operations[{index}].occurrence 必须是从 1 开始的整数"
                )
            if operation_name == "comment" and "quote" not in operation:
                raise DocumentEditError(
                    f"operations[{index}] 只有提供 quote 时才能指定 occurrence"
                )

    @staticmethod
    def _require_text(
        index: int,
        operation: dict[str, Any],
        field_name: str,
        *,
        allow_empty: bool,
    ) -> None:
        """校验操作字段是否为允许空值的字符串。

        Args:
            index: 操作在批次中的零基位置。
            operation: 包含待校验字段的操作字典。
            field_name: 需要校验的字段名称。
            allow_empty: 是否允许空字符串。

        Returns:
            None。

        Raises:
            DocumentEditError: 字段不是字符串或不允许为空。
        """
        value = operation[field_name]
        if not isinstance(value, str) or (not allow_empty and not value):
            requirement = "字符串" if allow_empty else "非空字符串"
            raise DocumentEditError(
                f"operations[{index}].{field_name} 必须是{requirement}"
            )

    @staticmethod
    def _require_non_negative_integer(
        index: int,
        operation: dict[str, Any],
        field_name: str,
    ) -> None:
        """校验操作字段是否为非负整数。

        Args:
            index: 操作在批次中的零基位置。
            operation: 包含待校验字段的操作字典。
            field_name: 需要校验的字段名称。

        Returns:
            None。

        Raises:
            DocumentEditError: 字段不是非负整数。
        """
        value = operation[field_name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DocumentEditError(f"operations[{index}].{field_name} 必须是非负整数")

    def _validate_target(self, index: int, operation_name: str, target: Any) -> None:
        """校验操作目标的 OOXML 类型和当前修订状态。

        Args:
            index: 操作在批次中的零基位置。
            operation_name: 当前操作名称。
            target: 内容块 ID 对应的 XML 元素。

        Returns:
            None。

        Raises:
            DocumentEditError: 操作不支持目标类型或已有修订嵌套风险。
        """
        paragraph_operations = {"replace_text", "comment"}
        if operation_name in paragraph_operations and target.tag != _qualified_name(
            "w:p"
        ):
            raise DocumentEditError(
                f"operations[{index}] 的 {operation_name} 只能用于段落 ID"
            )
        if operation_name == "set_cell" and target.tag != _qualified_name("w:tbl"):
            raise DocumentEditError(f"operations[{index}] 的 set_cell 只能用于表格 ID")

        revision_sensitive = {"replace_text", "delete", "set_cell"}
        if self.tracked and operation_name in revision_sensitive:
            has_existing_revision = (
                target.find(".//w:ins", _NAMESPACES) is not None
                or target.find(".//w:del", _NAMESPACES) is not None
            )
            if has_existing_revision:
                raise DocumentEditError(
                    f"operations[{index}] 的目标含有未处理修订，"
                    "当前不能继续嵌套 tracked 修改"
                )

    def _op_replace_text(
        self,
        paragraph: Any,
        *,
        old: str,
        new: str,
        occurrence: int | None = None,
    ) -> str:
        """在段落内跨文本 run 替换指定文字，并尽量保留原格式。

        Args:
            paragraph: 需要修改的 w:p 段落。
            old: 需要匹配的原文字。
            new: 替换后的文字，可以为空。
            occurrence: 多处匹配时选择的从 1 开始的位置。

        Returns:
            可返回给模型的操作结果。

        Raises:
            DocumentEditError: 原文字不存在、匹配不唯一或 occurrence 越界。
        """
        full_text, _ = self._run_map(paragraph)
        hits = [
            position
            for position in range(len(full_text))
            if full_text.startswith(old, position)
        ]
        if not hits:
            raise DocumentEditError(
                f"段落中找不到 {old!r}。当前可编辑文本：{full_text!r}"
            )
        if len(hits) > 1 and occurrence is None:
            raise DocumentEditError(
                f"{old!r} 在段落中出现 {len(hits)} 次，请指定 occurrence"
            )
        selected_occurrence = occurrence or 1
        if selected_occurrence > len(hits):
            raise DocumentEditError(
                f"occurrence={selected_occurrence} 越界，{old!r} 仅出现 {len(hits)} 次"
            )
        if old == new:
            return "原文字与新文字相同，未产生修改"

        match_start = hits[selected_occurrence - 1]
        common_prefix = 0
        while (
            common_prefix < min(len(old), len(new))
            and old[common_prefix] == new[common_prefix]
        ):
            common_prefix += 1
        common_suffix = 0
        suffix_limit = min(len(old), len(new)) - common_prefix
        while (
            common_suffix < suffix_limit
            and old[-1 - common_suffix] == new[-1 - common_suffix]
        ):
            common_suffix += 1

        change_start = match_start + common_prefix
        change_end = match_start + len(old) - common_suffix
        new_end = len(new) - common_suffix if common_suffix else len(new)
        replacement_text = new[common_prefix:new_end]

        if change_start == change_end:
            anchor_offset = 0 if change_start == 0 else change_start - 1
            isolated_runs = self._isolate(
                paragraph,
                anchor_offset,
                anchor_offset + 1,
            )
            if not isolated_runs:
                raise DocumentEditError("无法定位纯插入位置，请重新读取文档后重试")
            anchor = isolated_runs[0]
            run_properties = anchor.find("w:rPr", _NAMESPACES)
            new_run = self._new_run(replacement_text, run_properties)
            node = self._wrap_insertion(new_run) if self.tracked else new_run
            if change_start == 0:
                anchor.addprevious(node)
            else:
                anchor.addnext(node)
            return f"已替换 {old!r} -> {new!r}"

        isolated_runs = self._isolate(paragraph, change_start, change_end)
        if not isolated_runs:
            raise DocumentEditError("无法定位需要替换的文本 run，请重新读取文档后重试")
        run_properties = isolated_runs[0].find("w:rPr", _NAMESPACES)
        if self.tracked:
            last_deletion = None
            for run in isolated_runs:
                last_deletion = self._wrap_runs([run], "del")
            if replacement_text and last_deletion is not None:
                last_deletion.addnext(
                    self._wrap_insertion(
                        self._new_run(replacement_text, run_properties)
                    )
                )
        else:
            retained_run = isolated_runs[0] if replacement_text else None
            if retained_run is not None:
                self._set_run_text(retained_run, replacement_text)
            for run in isolated_runs:
                if run is not retained_run:
                    parent = run.getparent()
                    if parent is not None:
                        parent.remove(run)
        return f"已替换 {old!r} -> {new!r}"

    def _op_insert_after(
        self,
        reference: Any,
        *,
        target_id: str,
        text: str,
        style: str | None = None,
    ) -> str:
        """在指定正文块后插入新段落，并继承可安全复用的格式。

        Args:
            reference: 作为插入位置参考的正文 XML 元素。
            target_id: 参考元素在读取结果中的内容块 ID。
            text: 新段落文本。
            style: 可选的 Word 段落 styleId。

        Returns:
            可返回给模型的操作结果。
        """
        new_paragraph = etree.Element(_qualified_name("w:p"))
        run_properties = None
        if reference.tag == _qualified_name("w:p"):
            paragraph_properties = reference.find("w:pPr", _NAMESPACES)
            if paragraph_properties is not None:
                copied_properties = copy.deepcopy(paragraph_properties)
                section_properties = copied_properties.find("w:sectPr", _NAMESPACES)
                if section_properties is not None:
                    copied_properties.remove(section_properties)
                marker_properties = copied_properties.find("w:rPr", _NAMESPACES)
                if marker_properties is not None:
                    for revision_name in ("w:ins", "w:del"):
                        for revision in marker_properties.findall(
                            revision_name,
                            _NAMESPACES,
                        ):
                            marker_properties.remove(revision)
                new_paragraph.append(copied_properties)
            first_run = next(reference.iter(_qualified_name("w:r")), None)
            if first_run is not None:
                run_properties = first_run.find("w:rPr", _NAMESPACES)

        if style is not None:
            paragraph_properties = new_paragraph.find("w:pPr", _NAMESPACES)
            if paragraph_properties is None:
                paragraph_properties = etree.Element(_qualified_name("w:pPr"))
                new_paragraph.insert(0, paragraph_properties)
            style_element = paragraph_properties.find("w:pStyle", _NAMESPACES)
            if style_element is None:
                style_element = etree.Element(_qualified_name("w:pStyle"))
                paragraph_properties.insert(0, style_element)
            style_element.set(_qualified_name("w:val"), style)

        new_run = self._new_run(text, run_properties)
        if self.tracked:
            new_paragraph.append(self._wrap_insertion(new_run))
            self._mark_paragraph(new_paragraph, "ins")
        else:
            new_paragraph.append(new_run)

        insertion_anchor = self._insert_tails.get(target_id, reference)
        insertion_anchor.addnext(new_paragraph)
        self._insert_tails[target_id] = new_paragraph
        return "已插入段落"

    def _op_delete(self, element: Any) -> str:
        """直接删除正文块，或在 tracked 模式下标记段落和表格内容删除。

        Args:
            element: 需要删除的段落、表格或内容控件元素。

        Returns:
            可返回给模型的操作结果。

        Raises:
            DocumentEditError: 目标已经脱离文档树。
        """
        parent = element.getparent()
        if parent is None:
            raise DocumentEditError("删除目标已经不在文档中")
        if not self.tracked:
            parent.remove(element)
            return "已删除"

        paragraphs = (
            [element]
            if element.tag == _qualified_name("w:p")
            else list(element.iter(_qualified_name("w:p")))
        )
        for paragraph in paragraphs:
            runs = [
                run
                for run in paragraph.iter(_qualified_name("w:r"))
                if not self._in_revision(run)
            ]
            for run in runs:
                self._wrap_runs([run], "del")
            self._mark_paragraph(paragraph, "del")

        if element.tag == _qualified_name("w:tbl"):
            for row in element.findall("w:tr", _NAMESPACES):
                row_properties = row.find("w:trPr", _NAMESPACES)
                if row_properties is None:
                    row_properties = etree.Element(_qualified_name("w:trPr"))
                    row.insert(0, row_properties)
                row_properties.append(self._revision("del"))
        # TODO: 后续补充内容控件和复杂嵌套表格的专用 tracked 删除语义。
        return "已标记删除"

    def _op_set_cell(
        self,
        table: Any,
        *,
        row: int,
        col: int,
        text: str,
    ) -> str:
        """按物理 w:tc 下标整体替换一个表格单元格的文本内容。

        Args:
            table: 需要修改的 w:tbl 表格。
            row: 从 0 开始的物理行下标。
            col: 从 0 开始的物理单元格下标，不展开合并单元格。
            text: 单元格的新文本，可以为空。

        Returns:
            可返回给模型的操作结果。

        Raises:
            DocumentEditError: 行或列超出表格范围。
        """
        rows = table.findall("w:tr", _NAMESPACES)
        if row >= len(rows):
            raise DocumentEditError(f"表格行 {row} 越界，当前表格共有 {len(rows)} 行")
        cells = rows[row].findall("w:tc", _NAMESPACES)
        if col >= len(cells):
            raise DocumentEditError(
                f"单元格 ({row}, {col}) 越界，第 {row} 行共有 {len(cells)} 个物理单元格"
            )
        cell = cells[col]
        paragraphs = cell.findall("w:p", _NAMESPACES)
        first_paragraph = paragraphs[0] if paragraphs else None
        paragraph_properties = (
            first_paragraph.find("w:pPr", _NAMESPACES)
            if first_paragraph is not None
            else None
        )
        first_run = (
            next(first_paragraph.iter(_qualified_name("w:r")), None)
            if first_paragraph is not None
            else None
        )
        run_properties = (
            first_run.find("w:rPr", _NAMESPACES) if first_run is not None else None
        )

        if not self.tracked:
            for child in list(cell):
                if child.tag != _qualified_name("w:tcPr"):
                    cell.remove(child)
            new_paragraph = etree.Element(_qualified_name("w:p"))
            if paragraph_properties is not None:
                new_paragraph.append(copy.deepcopy(paragraph_properties))
            new_paragraph.append(self._new_run(text, run_properties))
            cell.append(new_paragraph)
            return f"已设置单元格 ({row}, {col})"

        if first_paragraph is None:
            first_paragraph = etree.SubElement(cell, _qualified_name("w:p"))
            paragraphs = [first_paragraph]
        for extra_paragraph in paragraphs[1:]:
            self._op_delete(extra_paragraph)
        runs = [
            run
            for run in first_paragraph.iter(_qualified_name("w:r"))
            if not self._in_revision(run)
        ]
        for run in runs:
            self._wrap_runs([run], "del")
        first_paragraph.append(
            self._wrap_insertion(self._new_run(text, run_properties))
        )
        return f"已设置单元格 ({row}, {col})"

    def _op_comment(
        self,
        paragraph: Any,
        *,
        text: str,
        quote: str | None = None,
        occurrence: int | None = None,
    ) -> str:
        """为整个段落或段落内指定文字范围添加 Word 批注。

        Args:
            paragraph: 需要添加批注的 w:p 段落。
            text: 批注正文。
            quote: 可选的段内原文范围。
            occurrence: quote 多次出现时选择的从 1 开始的位置。

        Returns:
            可返回给模型的操作结果。

        Raises:
            DocumentEditError: quote 不存在、不唯一或无法定位到文本 run。
        """
        comment_id = str(self._allocate_id())
        range_start = etree.Element(_qualified_name("w:commentRangeStart"))
        range_start.set(_qualified_name("w:id"), comment_id)
        range_end = etree.Element(_qualified_name("w:commentRangeEnd"))
        range_end.set(_qualified_name("w:id"), comment_id)
        reference_run = etree.Element(_qualified_name("w:r"))
        reference_properties = etree.SubElement(
            reference_run,
            _qualified_name("w:rPr"),
        )
        reference_style = etree.SubElement(
            reference_properties,
            _qualified_name("w:rStyle"),
        )
        reference_style.set(_qualified_name("w:val"), "CommentReference")
        reference = etree.SubElement(
            reference_run,
            _qualified_name("w:commentReference"),
        )
        reference.set(_qualified_name("w:id"), comment_id)

        if quote is not None:
            full_text, _ = self._run_map(paragraph)
            hits = [
                position
                for position in range(len(full_text))
                if full_text.startswith(quote, position)
            ]
            if not hits:
                raise DocumentEditError(f"段落中找不到要批注的文字 {quote!r}")
            if len(hits) > 1 and occurrence is None:
                raise DocumentEditError(
                    f"{quote!r} 在段落中出现 {len(hits)} 次，请指定 occurrence"
                )
            selected_occurrence = occurrence or 1
            if selected_occurrence > len(hits):
                raise DocumentEditError(
                    f"occurrence={selected_occurrence} 越界，"
                    f"{quote!r} 仅出现 {len(hits)} 次"
                )
            start = hits[selected_occurrence - 1]
            runs = self._isolate(paragraph, start, start + len(quote))
            if not runs:
                raise DocumentEditError("无法定位批注文字对应的文本 run")
            runs[0].addprevious(range_start)
            runs[-1].addnext(range_end)
        else:
            paragraph_properties = paragraph.find("w:pPr", _NAMESPACES)
            if paragraph_properties is not None:
                paragraph_properties.addnext(range_start)
            else:
                paragraph.insert(0, range_start)
            paragraph.append(range_end)
        range_end.addnext(reference_run)
        self._add_comment_part(comment_id, text)
        return f"已添加批注 #{comment_id}"

    def _text_runs(self, paragraph: Any) -> list[Any]:
        """返回段落中包含普通文本且不位于删除修订内的 run。

        Args:
            paragraph: 需要扫描的 w:p 段落。

        Returns:
            按文档顺序排列的可编辑 w:r 列表。
        """
        return [
            run
            for run in paragraph.iter(_qualified_name("w:r"))
            if run.find("w:t", _NAMESPACES) is not None and not self._in_deletion(run)
        ]

    def _run_map(self, paragraph: Any) -> tuple[str, list[tuple[Any, int, int]]]:
        """拼接段落可编辑文本，并记录每个 run 对应的字符区间。

        Args:
            paragraph: 需要建立文本映射的 w:p 段落。

        Returns:
            完整文本和 ``(run, start, end)`` 区间列表。
        """
        spans: list[tuple[Any, int, int]] = []
        parts: list[str] = []
        position = 0
        for run in self._text_runs(paragraph):
            text = "".join(node.text or "" for node in run.findall("w:t", _NAMESPACES))
            spans.append((run, position, position + len(text)))
            parts.append(text)
            position += len(text)
        return "".join(parts), spans

    def _isolate(self, paragraph: Any, start: int, end: int) -> list[Any]:
        """将字符区间边界处的 run 拆开，返回完全覆盖该区间的 run。

        Args:
            paragraph: 包含目标文本的 w:p 段落。
            start: 目标字符区间起点，包含该位置。
            end: 目标字符区间终点，不包含该位置。

        Returns:
            恰好覆盖 ``[start, end)`` 的 run 列表。
        """
        _, spans = self._run_map(paragraph)
        isolated: list[Any] = []
        for run, run_start, run_end in spans:
            if run_end <= start or run_start >= end:
                continue
            if run_start < start:
                _, run = self._split_run(run, start - run_start)
                run_start = start
            if run_end > end:
                run, _ = self._split_run(run, end - run_start)
            isolated.append(run)
        return isolated

    def _split_run(self, run: Any, offset: int) -> tuple[Any, Any]:
        """在指定字符位置将一个文本 run 拆成格式相同的左右两部分。

        Args:
            run: 需要拆分的 w:r 元素。
            offset: 右侧文本在 run 文本中的起始位置。

        Returns:
            拆分后的左 run 和右 run。
        """
        text = "".join(node.text or "" for node in run.findall("w:t", _NAMESPACES))
        right_run = copy.deepcopy(run)
        self._set_run_text(run, text[:offset])
        self._set_run_text(right_run, text[offset:])
        run.addnext(right_run)
        return run, right_run

    @staticmethod
    def _set_run_text(run: Any, text: str) -> None:
        """将一个 run 的直接 w:t 子节点规范为给定文本。

        Args:
            run: 需要更新的 w:r 元素。
            text: 新文本，可以为空。

        Returns:
            None。
        """
        text_nodes = run.findall("w:t", _NAMESPACES)
        for extra_node in text_nodes[1:]:
            run.remove(extra_node)
        text_node = (
            text_nodes[0]
            if text_nodes
            else etree.SubElement(run, _qualified_name("w:t"))
        )
        text_node.text = text
        text_node.set(_XML_SPACE, "preserve")

    def _new_run(self, text: str, run_properties: Any | None = None) -> Any:
        """创建一个可选继承字符格式的新文本 run。

        Args:
            text: 新 run 的文本。
            run_properties: 可选的原 w:rPr 字符格式节点。

        Returns:
            新建的 w:r 元素。
        """
        run = etree.Element(_qualified_name("w:r"))
        if run_properties is not None:
            run.append(copy.deepcopy(run_properties))
        self._set_run_text(run, text)
        return run

    def _revision(self, kind: str) -> Any:
        """创建带作者、时间和唯一 ID 的修订节点。

        Args:
            kind: 修订节点本地名称，例如 ins 或 del。

        Returns:
            新建的修订 XML 元素。
        """
        revision = etree.Element(_qualified_name(f"w:{kind}"))
        revision.set(_qualified_name("w:id"), str(self._allocate_id()))
        revision.set(_qualified_name("w:author"), self.author)
        revision.set(_qualified_name("w:date"), self.timestamp)
        return revision

    def _wrap_insertion(self, run: Any) -> Any:
        """将一个 run 包装为插入修订。

        Args:
            run: 需要标记为插入的 w:r 元素。

        Returns:
            包含该 run 的 w:ins 元素。
        """
        insertion = self._revision("ins")
        insertion.append(run)
        return insertion

    def _wrap_runs(self, runs: list[Any], kind: str) -> Any:
        """将同一位置的一组 run 包装为插入或删除修订。

        Args:
            runs: 需要包裹的非空 w:r 列表。
            kind: 修订类型，当前使用 ins 或 del。

        Returns:
            新建并插入文档树的修订包装节点。
        """
        wrapper = self._revision(kind)
        runs[0].addprevious(wrapper)
        for run in runs:
            if kind == "del":
                for text_node in run.findall("w:t", _NAMESPACES):
                    text_node.tag = _qualified_name("w:delText")
            wrapper.append(run)
        return wrapper

    def _mark_paragraph(self, paragraph: Any, kind: str) -> None:
        """在段落标记格式中记录整段插入或删除修订。

        Args:
            paragraph: 需要标记的 w:p 段落。
            kind: ins 或 del 修订类型。

        Returns:
            None。
        """
        paragraph_properties = paragraph.find("w:pPr", _NAMESPACES)
        if paragraph_properties is None:
            paragraph_properties = etree.Element(_qualified_name("w:pPr"))
            paragraph.insert(0, paragraph_properties)
        marker_properties = paragraph_properties.find("w:rPr", _NAMESPACES)
        if marker_properties is None:
            marker_properties = etree.SubElement(
                paragraph_properties,
                _qualified_name("w:rPr"),
            )
        marker_properties.insert(0, self._revision(kind))

    @staticmethod
    def _in_deletion(element: Any) -> bool:
        """判断元素是否位于删除修订内。

        Args:
            element: 需要检查的 OOXML 元素。

        Returns:
            位于 w:del 祖先内时返回 True。
        """
        return any(
            ancestor.tag == _qualified_name("w:del")
            for ancestor in element.iterancestors()
        )

    @staticmethod
    def _in_revision(element: Any) -> bool:
        """判断元素是否已经位于插入或删除修订内。

        Args:
            element: 需要检查的 OOXML 元素。

        Returns:
            位于 w:ins 或 w:del 祖先内时返回 True。
        """
        revision_tags = {_qualified_name("w:ins"), _qualified_name("w:del")}
        return any(
            ancestor.tag in revision_tags for ancestor in element.iterancestors()
        )

    def _maximum_existing_id(self) -> int:
        """查找正文修订、批注和书签使用过的最大非负整数 ID。

        Returns:
            已存在的最大 ID；没有可用 ID 时返回 0。
        """
        identifiers = [0]
        for tag in (
            "w:ins",
            "w:del",
            "w:commentRangeStart",
            "w:bookmarkStart",
        ):
            for element in self.document.iter(_qualified_name(tag)):
                value = element.get(_qualified_name("w:id"))
                if value and value.isdigit():
                    identifiers.append(int(value))
        identifiers.extend(
            int(comment_id)
            for comment_id in self.parser.comments
            if comment_id.isdigit()
        )
        return max(identifiers)

    def _allocate_id(self) -> int:
        """分配本批修改中唯一的非负整数 ID。

        Returns:
            当前分配的 ID。
        """
        allocated = self._next_id
        self._next_id += 1
        return allocated

    def _load_part(self, part_name: str) -> Any | None:
        """从待写部件或源 DOCX 安全解析一个 XML 部件。

        Args:
            part_name: DOCX ZIP 内的部件路径。

        Returns:
            XML 根节点；部件不存在时返回 None。
        """
        if part_name in self.extra_parts:
            data = self.extra_parts[part_name]
        else:
            try:
                data = self.archive.read(part_name)
            except KeyError:
                return None
        xml_parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
            recover=False,
        )
        return etree.fromstring(data, parser=xml_parser)

    def _add_comment_part(self, comment_id: str, text: str) -> None:
        """新增批注正文，并确保 DOCX 已注册批注部件。

        Args:
            comment_id: 与正文批注范围一致的批注 ID。
            text: 批注正文。

        Returns:
            None。

        Raises:
            DocumentEditError: DOCX 缺少内容类型部件。
        """
        comments_root = self._load_part(_COMMENTS_PART)
        if comments_root is None:
            comments_root = etree.Element(
                _qualified_name("w:comments"),
                nsmap={"w": _NAMESPACES["w"]},
            )
        self._ensure_comments_registered()

        comment = etree.SubElement(comments_root, _qualified_name("w:comment"))
        comment.set(_qualified_name("w:id"), comment_id)
        comment.set(_qualified_name("w:author"), self.author)
        comment.set(_qualified_name("w:date"), self.timestamp)
        comment.set(_qualified_name("w:initials"), self.author[:2])
        paragraph = etree.SubElement(comment, _qualified_name("w:p"))
        paragraph.append(self._new_run(text))
        self.extra_parts[_COMMENTS_PART] = self._serialize(comments_root)
        # TODO: 后续补充 commentsExtended.xml 等新版 Word 批注扩展部件。

    def _ensure_comments_registered(self) -> None:
        """确保 document.xml.rels 和 [Content_Types].xml 注册批注部件。

        Returns:
            None。

        Raises:
            DocumentEditError: DOCX 缺少必需的内容类型部件。
        """
        relationships = self._load_part(_DOCUMENT_RELATIONSHIPS_PART)
        if relationships is None:
            relationships = etree.Element(
                f"{{{_PACKAGE_RELATIONSHIPS_NAMESPACE}}}Relationships",
                nsmap={None: _PACKAGE_RELATIONSHIPS_NAMESPACE},
            )
        has_comments_relationship = any(
            relationship.get("Type") == _COMMENTS_RELATIONSHIP_TYPE
            for relationship in relationships
        )
        if not has_comments_relationship:
            existing_ids = {relationship.get("Id") for relationship in relationships}
            relationship_id = next(
                f"rId{index}"
                for index in range(1, 100_000)
                if f"rId{index}" not in existing_ids
            )
            relationship = etree.SubElement(
                relationships,
                f"{{{_PACKAGE_RELATIONSHIPS_NAMESPACE}}}Relationship",
            )
            relationship.set("Id", relationship_id)
            relationship.set("Type", _COMMENTS_RELATIONSHIP_TYPE)
            relationship.set("Target", "comments.xml")
            self.extra_parts[_DOCUMENT_RELATIONSHIPS_PART] = self._serialize(
                relationships
            )

        content_types = self._load_part(_CONTENT_TYPES_PART)
        if content_types is None:
            raise DocumentEditError("DOCX 缺少必需部件: [Content_Types].xml")
        has_comments_content_type = any(
            child.get("PartName") == "/word/comments.xml" for child in content_types
        )
        if not has_comments_content_type:
            content_types_namespace = etree.QName(content_types).namespace
            override_tag = (
                f"{{{content_types_namespace}}}Override"
                if content_types_namespace
                else "Override"
            )
            override = etree.SubElement(content_types, override_tag)
            override.set("PartName", "/word/comments.xml")
            override.set("ContentType", _COMMENTS_CONTENT_TYPE)
            self.extra_parts[_CONTENT_TYPES_PART] = self._serialize(content_types)

    @staticmethod
    def _serialize(root: Any) -> bytes:
        """将修改后的 OOXML 根节点序列化为 UTF-8 XML。

        Args:
            root: 需要序列化的 XML 根节点。

        Returns:
            带 XML 声明的字节内容。
        """
        return etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )
