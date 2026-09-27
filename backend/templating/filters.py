"""提示词模板过滤器：把多行文本压成单行、按“保留首尾”截断，也供代码直接复用。"""

from __future__ import annotations

from typing import Any

_MIDDLE_MARKER = "…[中间已省略]…"  # 首尾截断时插入的省略标记。


def oneline(text: Any) -> str:
    """把文本中的所有空白（含换行）折叠为单个空格，避免破坏外层排版层级。

    Args:
        text: 任意文本。

    Returns:
        单行文本。
    """
    return " ".join(str(text).split())


def truncate_middle(text: Any, max_chars: int) -> str:
    """超长时保留开头和结尾各一半，中间以省略标记替代。

    Args:
        text: 任意文本。
        max_chars: 结果允许的最大字符数（含省略标记）。

    Returns:
        未超限的原文，或首尾保留的截断文本。
    """
    value = str(text)
    if len(value) <= max_chars:
        return value
    if max_chars <= len(_MIDDLE_MARKER):
        return value[:max_chars]
    keep = max_chars - len(_MIDDLE_MARKER)
    head = (keep + 1) // 2
    tail = keep - head
    return f"{value[:head]}{_MIDDLE_MARKER}{value[len(value) - tail :]}"
