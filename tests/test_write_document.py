"""WriteDocumentTool 独立测试，验证 DOCX 修改、修订和版本保护。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from backend.tools import ReadDocumentTool, WriteDocumentTool

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_DOCUMENT = (
    _PROJECT_ROOT
    / "backend"
    / "data_agent"
    / "tenant_packs"
    / "233"
    / "dbh"
    / "PE.docx"
)
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NAMESPACES = {"w": _WORD_NAMESPACE}


def _qualified_name(local_name: str) -> str:
    """生成测试 OOXML 元素使用的 WordprocessingML 完整名称。

    Args:
        local_name: 不带命名空间前缀的元素本地名称。

    Returns:
        lxml 使用的 ``{namespace}name`` 完整名称。
    """
    return f"{{{_WORD_NAMESPACE}}}{local_name}"


def _copy_sample(tmp_path: Path, filename: str) -> Path:
    """将只读样本文档复制到 pytest 临时目录。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。
        filename: 临时 DOCX 文件名。

    Returns:
        可安全修改的临时 DOCX 路径。
    """
    destination = tmp_path / filename
    shutil.copy2(_SAMPLE_DOCUMENT, destination)
    return destination


def _find_block_id(markdown: str, expected_text: str) -> str:
    """根据 Markdown 中的可见文本查找对应内容块 ID。

    Args:
        markdown: ReadDocumentTool 返回的带 ID Markdown。
        expected_text: 用于确定目标内容块的唯一可见文本。

    Returns:
        与目标文本位于同一行的内容块 ID。

    Raises:
        AssertionError: 没有找到目标文本或该行不包含内容块 ID。
    """
    for line in markdown.splitlines():
        if expected_text not in line:
            continue
        match = re.match(r"^(?:#{1,9} )?\[([^]]+)]", line)
        assert match is not None, f"目标文本所在行缺少内容块 ID: {line}"
        return match.group(1)
    raise AssertionError(f"Markdown 中没有找到目标文本: {expected_text}")


def _create_document_with_table(tmp_path: Path) -> Path:
    """基于样本文档派生一个包含单单元格表格的 DOCX。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        包含 ``old cell`` 表格文本的临时 DOCX 路径。
    """
    destination = tmp_path / "table.docx"
    with ZipFile(_SAMPLE_DOCUMENT) as source_archive:
        document = etree.fromstring(source_archive.read("word/document.xml"))
        body = document.find("w:body", _NAMESPACES)
        assert body is not None

        table = etree.Element(_qualified_name("tbl"))
        row = etree.SubElement(table, _qualified_name("tr"))
        cell = etree.SubElement(row, _qualified_name("tc"))
        paragraph = etree.SubElement(cell, _qualified_name("p"))
        run = etree.SubElement(paragraph, _qualified_name("r"))
        text = etree.SubElement(run, _qualified_name("t"))
        text.text = "old cell"

        section_properties = body.find("w:sectPr", _NAMESPACES)
        insertion_index = (
            body.index(section_properties)
            if section_properties is not None
            else len(body)
        )
        body.insert(insertion_index, table)
        document_bytes = etree.tostring(
            document,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )

        with ZipFile(destination, "w", allowZip64=True) as destination_archive:
            destination_archive.comment = source_archive.comment
            for item in source_archive.infolist():
                data = (
                    document_bytes
                    if item.filename == "word/document.xml"
                    else source_archive.read(item.filename)
                )
                destination_archive.writestr(item, data)
    return destination


def test_write_document_direct_operations_to_new_file(tmp_path: Path) -> None:
    """验证直接模式能另存并执行替换、插入段落和添加批注。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        None；修改结果不符合预期时由断言报告失败。
    """
    source_path = _copy_sample(tmp_path, "source.docx")
    reader = ReadDocumentTool(tmp_path)
    writer = WriteDocumentTool(tmp_path)
    source_before = reader.execute(path=source_path.name, max_chars=100_000)
    target_id = _find_block_id(source_before["markdown"], "Preparation of Articles")

    result = writer.execute(
        path=source_path.name,
        output_path="edited.docx",
        revision=source_before["revision"],
        mode="direct",
        operations=[
            {
                "op": "replace_text",
                "id": target_id,
                "old": "2022",
                "new": "2026",
            },
            {
                "op": "insert_after",
                "id": target_id,
                "text": "Seshat write tool inserted paragraph.",
            },
            {
                "op": "comment",
                "id": target_id,
                "text": "Please review the publication year.",
                "quote": "IEEE",
            },
        ],
    )

    source_after = reader.execute(path=source_path.name, max_chars=100_000)
    edited = reader.execute(path="edited.docx", max_chars=100_000)
    assert "2022" in source_after["markdown"]
    assert "2026" in edited["markdown"]
    assert "Seshat write tool inserted paragraph." in edited["markdown"]
    assert any(
        comment["text"] == "Please review the publication year."
        for comment in edited["comments"]
    )
    assert result["operation_count"] == 3
    assert result["previous_revision"] == source_before["revision"]
    assert result["revision"] == edited["revision"]
    assert result["path"] == "edited.docx"


def test_write_document_tracked_replace_can_be_read_back(tmp_path: Path) -> None:
    """验证修订模式替换能被 accepted 和 markup 两种视图正确读取。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        None；修订结构或回读结果不符合预期时由断言报告失败。
    """
    document_path = _copy_sample(tmp_path, "tracked.docx")
    reader = ReadDocumentTool(tmp_path)
    writer = WriteDocumentTool(tmp_path)
    before = reader.execute(path=document_path.name, max_chars=100_000)
    target_id = _find_block_id(before["markdown"], "Preparation of Articles")

    result = writer.execute(
        path=document_path.name,
        revision=before["revision"],
        mode="tracked",
        author="Test Agent",
        operations=[
            {
                "op": "replace_text",
                "id": target_id,
                "old": "2022",
                "new": "2027",
            }
        ],
    )

    accepted = reader.execute(
        path=document_path.name,
        mode="accepted",
        max_chars=100_000,
    )
    markup = reader.execute(
        path=document_path.name,
        mode="markup",
        max_chars=100_000,
    )
    assert "2027" in accepted["markdown"]
    assert "[-2-]" in markup["markdown"]
    assert "{+7+}" in markup["markdown"]
    assert markup["metadata"]["changed_block_count"] >= 1
    assert result["mode"] == "tracked"
    assert result["author"] == "Test Agent"


def test_write_document_delete_removes_target_block(tmp_path: Path) -> None:
    """验证直接删除操作会移除指定正文内容块。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        None；目标块仍能被读取时由断言报告失败。
    """
    document_path = _copy_sample(tmp_path, "delete.docx")
    reader = ReadDocumentTool(tmp_path)
    writer = WriteDocumentTool(tmp_path)
    before = reader.execute(path=document_path.name, max_chars=100_000)
    target_text = "Preparation of Articles for IEEE TRANSACTIONS and JOURNALS"
    target_id = _find_block_id(before["markdown"], target_text)

    result = writer.execute(
        path=document_path.name,
        revision=before["revision"],
        mode="direct",
        operations=[{"op": "delete", "id": target_id}],
    )

    after = reader.execute(path=document_path.name, max_chars=100_000)
    assert target_text not in after["markdown"]
    assert result["operations"][0]["message"] == "已删除"


def test_write_document_set_cell_updates_table_text(tmp_path: Path) -> None:
    """验证 set_cell 能按物理行列更新表格单元格并被读取器回读。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        None；表格内容没有正确更新时由断言报告失败。
    """
    document_path = _create_document_with_table(tmp_path)
    reader = ReadDocumentTool(tmp_path)
    writer = WriteDocumentTool(tmp_path)
    before = reader.execute(path=document_path.name, max_chars=100_000)
    table_id = next(
        block["id"] for block in before["index"] if block["kind"] == "table"
    )

    result = writer.execute(
        path=document_path.name,
        revision=before["revision"],
        mode="direct",
        operations=[
            {
                "op": "set_cell",
                "id": table_id,
                "row": 0,
                "col": 0,
                "text": "new cell",
            }
        ],
    )

    after = reader.execute(path=document_path.name, max_chars=100_000)
    assert "new cell" in after["markdown"]
    assert "old cell" not in after["markdown"]
    assert result["operations"][0]["op"] == "set_cell"


def test_write_document_rejects_stale_revision_without_changing_file(
    tmp_path: Path,
) -> None:
    """验证 revision 不匹配时拒绝修改并保持源文件字节不变。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        None；工具未拒绝旧版本或改变了文件时由断言报告失败。
    """
    document_path = _copy_sample(tmp_path, "stale.docx")
    reader = ReadDocumentTool(tmp_path)
    writer = WriteDocumentTool(tmp_path)
    before = reader.execute(path=document_path.name, max_chars=100_000)
    target_id = _find_block_id(before["markdown"], "Preparation of Articles")
    original_bytes = document_path.read_bytes()

    with pytest.raises(ValueError, match="重新调用 read_document"):
        writer.execute(
            path=document_path.name,
            revision="0" * 64,
            mode="direct",
            operations=[
                {
                    "op": "replace_text",
                    "id": target_id,
                    "old": "2022",
                    "new": "2028",
                }
            ],
        )

    assert document_path.read_bytes() == original_bytes
