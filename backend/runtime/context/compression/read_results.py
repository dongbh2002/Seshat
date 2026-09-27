"""read_document 结果的解析与按块清理，供各压缩策略复用。

读取结果带 index（每块的 id、offset、length），据此可只清理部分块：被清理的连续块
合并为一行占位说明、索引项标记 cleaned 原因；全部块清理后整条替换为可重读的引用。
本模块只做消息级的纯变换，不涉及预算与统计。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.runtime.context.compression.base import tool_call_names

READ_TOOL_NAME = "read_document"  # 被按块清理的读取工具名称。
# 整条清理时，多种原因并存取最先出现者：越靠前越需要让模型知道信息可能丢失。
_REASON_PRIORITY = ("forced", "stale", "turn_ended", "reviewed", "superseded")
_REREAD_HINT = (  # 正文被清理后告诉模型如何按需找回。
    "需要原文时，按审阅工作状态大纲中的章节首尾块 ID 用 start_id / end_id 重读。"
)
_RECOVERY_MESSAGES = {  # 读取结果整条被清理时给模型的说明。
    "reviewed": "正文已清理，结论以审阅工作状态为准。",
    "turn_ended": "该轮对话已结束，正文已清理，结论见当时的回复与审阅工作状态。",
    "stale": "文档在这次读取之后已被修改，此为旧版本，正文已清理。",
    "superseded": "这些块在之后的读取中有更新的副本，此处已清理。",
    "forced": "正文在记录审阅前因上下文超限被强制清理，相关结论可能丢失。",
}
_REASON_LABELS = {  # 按块清理时占位行中的原因说明。
    "reviewed": "已审阅",
    "turn_ended": "所在轮次已结束",
    "stale": "读取后已被修改",
    "superseded": "之后有更新的读取",
    "forced": "未记录即被强制清理",
}


@dataclass
class BlockCleanup:
    """一次按块清理的结果。"""

    message: dict[str, Any]  # 清理后的工具消息。
    cleaned: dict[str, int]  # 本次新清理的块数，按原因统计。
    compacted: bool  # 是否已整条替换为引用。


def find_reads(
    messages: Sequence[Mapping[str, Any]],
    end: int,
) -> list[tuple[int, dict[str, Any]]]:
    """列出 messages[:end] 中仍含正文的读取结果，按从旧到新排列。

    Args:
        messages: 全部消息（用于解析工具调用名称）。
        end: 只检查此下标之前的消息。

    Returns:
        ``(消息下标, 已解析结果)`` 列表。
    """
    names = tool_call_names(messages)
    reads: list[tuple[int, dict[str, Any]]] = []
    for index in range(end):
        result = parse_read(messages[index], names)
        if result is not None:
            reads.append((index, result))
    return reads


def parse_read(
    message: Mapping[str, Any],
    tool_names: Mapping[str, str],
) -> dict[str, Any] | None:
    """解析仍含正文（完整或部分）的读取结果。

    Args:
        message: 任意消息。
        tool_names: 工具调用 ID 到工具名称的映射。

    Returns:
        含 markdown 与 index 的结果对象；不是读取结果或已整条清理时为 None。
    """
    if (
        message.get("role") != "tool"
        or tool_names.get(message.get("tool_call_id")) != READ_TOOL_NAME
    ):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return None
    if (
        isinstance(result, dict)
        and isinstance(result.get("markdown"), str)
        and isinstance(result.get("index"), list)
    ):
        return result
    return None


def remaining_ids(result: Mapping[str, Any]) -> list[str]:
    """返回读取结果中正文尚未被清理的块 ID。

    Args:
        result: 已解析的读取结果。

    Returns:
        按文档顺序排列的块 ID。
    """
    return [
        item["id"]
        for item in result["index"]
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and not item.get("cleaned")
    ]


def result_block_ids(result: Mapping[str, Any]) -> list[str]:
    """取出读取类结果涉及的全部块 ID：优先 index，整条清理后用 returned_block_ids。

    Args:
        result: 工具返回结果对象。

    Returns:
        按顺序排列的块 ID；不是读取类结果时为空列表。
    """
    index = result.get("index")
    if isinstance(index, list):
        return [
            item["id"]
            for item in index
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        ]
    returned = result.get("returned_block_ids")
    if isinstance(returned, list):
        return [block_id for block_id in returned if isinstance(block_id, str)]
    return []


def describe_read(
    message: Mapping[str, Any],
    result: Mapping[str, Any],
    block_ids: Sequence[str],
) -> dict[str, Any]:
    """生成供上下文提示使用的读取范围描述。

    Args:
        message: 读取结果所在的工具消息。
        result: 已解析的读取结果。
        block_ids: 需要提示的块 ID（如尚未审阅的块）。

    Returns:
        含 tool_call_id、path、first_id、last_id、count 的字典。
    """
    return {
        "tool_call_id": message.get("tool_call_id"),
        "path": result.get("path"),
        "first_id": block_ids[0] if block_ids else None,
        "last_id": block_ids[-1] if block_ids else None,
        "count": len(block_ids),
    }


def clean_blocks(
    message: Mapping[str, Any],
    result: Mapping[str, Any],
    removals: Mapping[str, str],
) -> BlockCleanup | None:
    """按块清理一次读取的正文，块全部清理后替换为整条引用。

    Args:
        message: 读取结果所在的工具消息。
        result: 已解析的读取结果。
        removals: 本次要清理的块 ID 到原因的映射。

    Returns:
        清理结果；缺少位置信息、无法部分清理时为 None。
    """
    items = [item for item in result["index"] if isinstance(item, dict)]
    reasons = {
        item["id"]: item.get("cleaned") or removals.get(item["id"]) for item in items
    }
    kept = [item for item in items if not reasons[item["id"]]]
    if kept and not all(isinstance(item.get("offset"), int) for item in kept):
        return None
    cleaned: dict[str, int] = {}
    for item in items:
        reason = removals.get(item["id"])
        if reason and not item.get("cleaned"):
            cleaned[reason] = cleaned.get(reason, 0) + 1
    if not kept:
        present = set(reasons.values())
        reason = next(name for name in _REASON_PRIORITY if name in present)
        return BlockCleanup(
            message=_compact(message, result, reason=reason),
            cleaned=cleaned,
            compacted=True,
        )
    markdown, new_items = _rebuild_markdown(result["markdown"], items, reasons)
    new_result = {
        **result,
        "markdown": markdown,
        "index": new_items,
        "partially_compacted": True,
        "compaction_note": f"部分块正文已清理，见占位行。{_REREAD_HINT}",
    }
    return BlockCleanup(
        message={**message, "content": _dumps(new_result)},
        cleaned=cleaned,
        compacted=False,
    )


def _rebuild_markdown(
    markdown: str,
    items: Sequence[dict[str, Any]],
    reasons: Mapping[str, str | None],
) -> tuple[str, list[dict[str, Any]]]:
    """用保留块原文和清理占位行重建 markdown，并重算位置。

    Args:
        markdown: 原 markdown。
        items: 全部块索引项（保留块带有效 offset / length）。
        reasons: 块 ID 到清理原因；保留块为 None。

    Returns:
        新 markdown 与新索引项列表。
    """
    segments: list[str] = []
    new_items: list[dict[str, Any]] = []
    run: list[str] = []
    run_reason: str | None = None
    position = 0

    def append_segment(text: str) -> int:
        """追加一段文本（段间两个换行）。

        Args:
            text: 要追加的文本。

        Returns:
            该段在新 markdown 中的起始位置。
        """
        nonlocal position
        start = position + (2 if segments else 0)
        segments.append(text)
        position = start + len(text)
        return start

    def flush_run() -> None:
        """把累积的同原因清理块写成一行占位说明。

        Returns:
            None。
        """
        nonlocal run, run_reason
        if not run or run_reason is None:
            return
        label = _REASON_LABELS[run_reason]
        text = (
            f"[{run[0]}] （正文已清理：{label}）"
            if len(run) == 1
            else f"[{run[0]}] ~ [{run[-1]}] （{len(run)} 块正文已清理：{label}）"
        )
        append_segment(text)
        run, run_reason = [], None

    for item in items:
        reason = reasons[item["id"]]
        if reason:
            if run and run_reason != reason:
                flush_run()
            run.append(item["id"])
            run_reason = reason
            new_items.append({**item, "cleaned": reason, "offset": None, "length": 0})
            continue
        flush_run()
        text = markdown[item["offset"] : item["offset"] + item["length"]]
        start = append_segment(text)
        new_items.append({**item, "offset": start, "length": len(text)})
    flush_run()
    return "\n\n".join(segments), new_items


def _compact(
    message: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """将读取结果整条替换为可重新读取的轻量引用。

    Args:
        message: 原始工具消息。
        result: 已解析的读取结果。
        reason: 清理原因，取值见 _RECOVERY_MESSAGES。

    Returns:
        保留原工具调用 ID 和定位信息的压缩消息。
    """
    compacted_result = {
        "tool": READ_TOOL_NAME,
        "compacted": True,
        "path": result.get("path"),
        "revision": result.get("revision"),
        "mode": result.get("mode"),
        "view": result.get("view"),
        "returned_block_ids": result_block_ids(result),
        "next_id": result.get("next_id"),
        "metadata": result.get("metadata"),
        "warnings": result.get("warnings"),
        "reason": reason,
        "recovery": f"{_RECOVERY_MESSAGES[reason]}{_REREAD_HINT}",
    }
    return {**message, "content": _dumps(compacted_result)}


def _dumps(value: Any) -> str:
    """紧凑序列化为 JSON（与 token 估算口径一致）。

    Args:
        value: 可序列化对象。

    Returns:
        JSON 字符串。
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
