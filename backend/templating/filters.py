"""提示词模板过滤器：把多行文本压成单行、按“保留首尾”截断、标出两段文本的差异，也供代码直接复用。"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

_MIDDLE_MARKER = "…[中间已省略]…"  # 首尾截断时插入的省略标记。
_TOKEN_PATTERN = re.compile(
    r"[A-Za-z0-9_]+|\s+|."
)  # 差异比较的词元：拉丁词、空白或单字。


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


def inline_diff(before: Any, after: Any, context_chars: int) -> str:
    """把修改前后两段文本合成一行差异：删除记为 [-…-]，插入记为 {+…+}，未改动处只保留邻近上下文。

    按词比较拉丁文字、按字比较中日韩文字，避免英文单词被拆成零散字母。

    Args:
        before: 修改前文本。
        after: 修改后文本。
        context_chars: 每处改动两侧保留的未改动字符数。

    Returns:
        单行差异文本；两段相同时返回原文。
    """
    old_tokens = _TOKEN_PATTERN.findall(oneline(before))
    new_tokens = _TOKEN_PATTERN.findall(oneline(after))
    if old_tokens == new_tokens:
        return "".join(old_tokens)
    opcodes = SequenceMatcher(
        None, old_tokens, new_tokens, autojunk=False
    ).get_opcodes()
    parts: list[str] = []
    for index, (tag, old_start, old_end, new_start, new_end) in enumerate(opcodes):
        if tag == "equal":
            text = "".join(old_tokens[old_start:old_end])
            head = text[:context_chars] if index > 0 else ""
            tail = text[-context_chars:] if index < len(opcodes) - 1 else ""
            parts.append(
                text if len(head) + len(tail) >= len(text) else f"{head}…{tail}"
            )
            continue
        if old_end > old_start:
            parts.append(f"[-{''.join(old_tokens[old_start:old_end])}-]")
        if new_end > new_start:
            parts.append(f"{{+{''.join(new_tokens[new_start:new_end])}+}}")
    return "".join(parts)
