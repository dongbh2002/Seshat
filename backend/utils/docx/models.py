"""定义 DOCX 内容块模型，读取、修改和结构索引共用同一套块 ID。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DocumentBlock:
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
        """生成返回给模型的轻量结构索引，只含正文中看不出的信息。

        标题级别、列表类型和批注已体现在 Markdown 中，不再重复。

        Returns:
            含 id、kind（块类型）与 has_changes（是否含修订）的字典。
        """
        return {
            "id": self.block_id,
            "kind": self.kind,
            "has_changes": self.has_changes,
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
