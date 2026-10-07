"""DOCX 内容块对齐：把同一文档两个版本的内容块配对，供版本比较使用。

对齐分三步：稳定块 ID（来自 w14:paraId）相同直接配对；其余块按正文做序列
比对，正文相同的配对；剩下的在同一改动区间内按文本相似度配对，视为被改写。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from backend.utils.docx.parser import is_position_based_block_id


@dataclass(frozen=True)
class BlockPair:
    """两个版本间的一组对应内容块。"""

    old_id: str | None  # 旧版本中的块 ID；新增块为 None。
    new_id: str | None  # 新版本中的块 ID；被删除的块为 None。


def align_blocks(
    old: Sequence[tuple[str, str]],
    new: Sequence[tuple[str, str]],
    *,
    min_similarity: float,
) -> list[BlockPair]:
    """对齐两个版本的内容块。

    Args:
        old: 旧版本按文档顺序排列的 (块 ID, 正文)。
        new: 新版本按文档顺序排列的 (块 ID, 正文)。
        min_similarity: 未能按 ID 或正文对齐的块，文本相似度达到该值才视为同一块。

    Returns:
        先按新版本顺序列出新版本的全部块（无对应时 old_id 为 None），
        再按旧版本顺序列出被删除的块。
    """
    old_ids = {block_id for block_id, _ in old}
    matched: dict[str, str] = {  # 新块 ID 到旧块 ID。
        block_id: block_id
        for block_id, _ in new
        if block_id in old_ids and not is_position_based_block_id(block_id)
    }
    matched_old = set(matched.values())
    old_rest = [
        (block_id, text) for block_id, text in old if block_id not in matched_old
    ]
    new_rest = [(block_id, text) for block_id, text in new if block_id not in matched]

    matcher = SequenceMatcher(
        None,
        [_normalize(text) for _, text in old_rest],
        [_normalize(text) for _, text in new_rest],
        autojunk=False,
    )
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            for (old_id, _), (new_id, _) in zip(
                old_rest[old_start:old_end], new_rest[new_start:new_end]
            ):
                matched[new_id] = old_id
        elif tag == "replace":
            matched.update(
                _pair_by_similarity(
                    old_rest[old_start:old_end],
                    new_rest[new_start:new_end],
                    min_similarity,
                )
            )

    matched_old = set(matched.values())
    pairs = [
        BlockPair(old_id=matched.get(block_id), new_id=block_id) for block_id, _ in new
    ]
    pairs.extend(
        BlockPair(old_id=block_id, new_id=None)
        for block_id, _ in old
        if block_id not in matched_old
    )
    return pairs


def _pair_by_similarity(
    old: Sequence[tuple[str, str]],
    new: Sequence[tuple[str, str]],
    min_similarity: float,
) -> dict[str, str]:
    """在一个改动区间内按文本相似度贪心配对，每个新块取最相似且未被占用的旧块。

    Args:
        old: 区间内旧版本的 (块 ID, 正文)。
        new: 区间内新版本的 (块 ID, 正文)。
        min_similarity: 配对所需的最低相似度。

    Returns:
        新块 ID 到旧块 ID 的映射。
    """
    candidates: list[tuple[float, int, int]] = []
    for new_index, (_, new_text) in enumerate(new):
        for old_index, (_, old_text) in enumerate(old):
            matcher = SequenceMatcher(
                None, _normalize(old_text), _normalize(new_text), autojunk=False
            )
            if matcher.real_quick_ratio() < min_similarity:
                continue
            if matcher.quick_ratio() < min_similarity:
                continue
            ratio = matcher.ratio()
            if ratio >= min_similarity:
                candidates.append((ratio, new_index, old_index))

    pairs: dict[str, str] = {}
    used_old: set[int] = set()
    for _, new_index, old_index in sorted(candidates, reverse=True):
        new_id = new[new_index][0]
        if new_id in pairs or old_index in used_old:
            continue
        pairs[new_id] = old[old_index][0]
        used_old.add(old_index)
    return pairs


def _normalize(text: str) -> str:
    """合并空白，避免仅空白不同的块被判为不同。

    Args:
        text: 块正文。

    Returns:
        各段空白合并为单个空格后的文本。
    """
    return " ".join(text.split())
