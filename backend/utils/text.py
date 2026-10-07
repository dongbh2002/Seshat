"""通用文本比较：字符片段的覆盖度与 Jaccard 相似度（相关性、近似重复），最长连续相同片段（原文泄露检查）。忽略全部空白。"""

from __future__ import annotations

from difflib import SequenceMatcher


def char_ngrams(text: str, size: int) -> set[str]:
    """取去除空白后的连续字符片段。

    Args:
        text: 文本。
        size: 片段长度。

    Returns:
        片段集合；文本短于 size 时为整段文本（空文本为空集）。
    """
    normalized = "".join(text.split())
    if len(normalized) <= size:
        return {normalized} if normalized else set()
    return {
        normalized[index : index + size] for index in range(len(normalized) - size + 1)
    }


def containment(part: str, whole: str, size: int) -> float:
    """计算 part 的字符片段有多大比例出现在 whole 中。

    Args:
        part: 被衡量的短文本。
        whole: 参照的长文本。
        size: 片段长度。

    Returns:
        [0, 1] 之间的覆盖度；part 为空时为 0。
    """
    pieces = char_ngrams(part, size)
    if not pieces:
        return 0.0
    return len(pieces & char_ngrams(whole, size)) / len(pieces)


def longest_common_run(first: str, second: str) -> int:
    """计算两段文本（去除空白后）最长的连续相同片段长度。

    Args:
        first: 第一段文本。
        second: 第二段文本。

    Returns:
        最长连续相同的字符数。
    """
    left = "".join(first.split())
    right = "".join(second.split())
    match = SequenceMatcher(None, left, right, autojunk=False).find_longest_match(
        0, len(left), 0, len(right)
    )
    return match.size


def jaccard(first: str, second: str, size: int) -> float:
    """计算两段文本字符片段集合的 Jaccard 相似度。

    Args:
        first: 第一段文本。
        second: 第二段文本。
        size: 片段长度。

    Returns:
        [0, 1] 之间的相似度；任一文本为空时为 0。
    """
    left, right = char_ngrams(first, size), char_ngrams(second, size)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
