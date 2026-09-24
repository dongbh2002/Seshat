"""ReadDocumentTool 独立测试，验证并打印 DOCX 的结构化读取效果。"""

import json
from pathlib import Path

from backend.tools import ReadDocumentTool

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DOCUMENT_ROOT = (
    _PROJECT_ROOT / "backend" / "data_agent" / "tenant_packs" / "233" / "dbh"
)


def test_read_document_tool_with_sample_docx() -> None:
    """直接读取 PE.docx，打印结果并验证基础返回结构。

    Returns:
        None；读取失败或返回结构不完整时由断言报告失败。
    """
    tool = ReadDocumentTool(_DOCUMENT_ROOT)
    result = tool.execute(path="PE.docx", max_chars=100000)

    print("\n=== 文档元数据 ===")
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=2))
    print("\n=== 本次结构索引 ===")
    print(json.dumps(result["index"], ensure_ascii=False, indent=2))
    print("\n=== 批注 ===")
    print(json.dumps(result["comments"], ensure_ascii=False, indent=2))
    print("\n=== 页眉页脚及脚注尾注 ===")
    print(json.dumps(result["extras"], ensure_ascii=False, indent=2))
    print("\n=== 读取警告 ===")
    print(json.dumps(result["warnings"], ensure_ascii=False, indent=2))
    print(f"\n=== 下一次续读 ID：{result['next_id']} ===")
    print("\n=== Markdown 读取效果 ===")
    print(result["markdown"])

    assert result["path"] == "PE.docx"
    assert len(result["revision"]) == 64
    assert result["markdown"].strip()
    assert result["index"]
    assert result["metadata"]["block_count"] >= len(result["index"])
    assert f"[{result['index'][0]['id']}]" in result["markdown"]
