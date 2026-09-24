"""定义 DOCX 读取结果使用的内部内容块模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _DocumentBlock:
    """保存一个可展示给模型并可供后续修改定位的文档块。"""

    block_id: str  # 模型引用内容块时使用的稳定 ID。
    kind: str  # 内容块类型，例如 heading、paragraph、list_item 或 table。
    text: str  # 内容块的 Markdown 或纯文本内容。
    xml_index: int  # 内容块在 document.xml 的 body 子节点中的位置。
    level: int = 0  # 标题级别或列表缩进级别。
    style: str = ""  # Word 样式显示名或列表类型。
    para_id: str = ""  # Word 原生 w14:paraId，段落不存在时为空。
    has_changes: bool = False  # 内容块是否包含插入或删除修订。
    comments: list[dict[str, str]] = field(default_factory=list)  # 块关联的批注。

    def to_index(self) -> dict[str, Any]:
        """生成不重复正文内容的轻量结构索引。

        Returns:
            供后续修改工具定位 XML 节点的索引字典。
        """
        return {
            "id": self.block_id,
            "kind": self.kind,
            "level": self.level,
            "style": self.style,
            "para_id": self.para_id,
            "xml_index": self.xml_index,
            "has_changes": self.has_changes,
            "comment_count": len(self.comments),
        }

    def to_markdown(self) -> str:
        """将内容块转换为带定位 ID 的 Markdown。

        Returns:
            适合模型阅读的 Markdown 文本。
        """
        marker = f"[{self.block_id}]"
        if self.kind == "heading":
            line = f"{'#' * self.level} {marker} {self.text}"
        elif self.kind == "list_item":
            bullet = "1." if self.style == "ordered" else "-"
            line = f"{'  ' * self.level}{bullet} {marker} {self.text}"
        elif self.kind == "table":
            line = f"{marker}\n{self.text}"
        else:
            line = f"{marker} {self.text}"

        for comment in self.comments:
            author = comment.get("author") or "未知作者"
            line += f"\n  > 批注（{author}）：{comment.get('text', '')}"
        return line
