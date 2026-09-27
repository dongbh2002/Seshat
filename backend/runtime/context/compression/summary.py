"""历史摘要：把归档轮次按规则提取为条目，并渲染为不超过上限的摘要文本。

每轮条目含用户请求、最终回复和不含正文的工具事实；排版由模板决定。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from backend.runtime.context.compression.base import (
    Context,
    HistoryArchive,
    tool_call_names,
)
from backend.runtime.context.compression.read_results import result_block_ids
from backend.runtime.context.token_estimator import TokenEstimator
from backend.templating import PromptRenderer, oneline, truncate_middle

_TEMPLATE = "history_summary_prompt.j2"  # 历史摘要模板。
_TRUNCATED_MARKER = "…[已截断]"  # 工具记录字段截断标记。


class HistorySummarizer:
    """按规则提取轮次条目，并在 token 上限内渲染历史摘要。"""

    def __init__(
        self,
        renderer: PromptRenderer,
        estimator: TokenEstimator,
        max_tokens: int,
    ) -> None:
        """初始化历史摘要器。

        Args:
            renderer: 渲染摘要模板的渲染器。
            estimator: token 估算器。
            max_tokens: 摘要文本的 token 上限。

        Returns:
            None。
        """
        self.renderer = renderer  # 摘要模板渲染器。
        self.estimator = estimator  # token 估算器。
        self.max_tokens = max_tokens  # 摘要 token 上限。

    def summarize_turn(self, number: int, turn: Context) -> dict[str, Any]:
        """提取一个轮次的用户请求、最终回复和工具事实。

        Args:
            number: 归档轮次编号。
            turn: 一个完整用户轮次中的消息。

        Returns:
            含 number、user、assistant、tools 的模板变量。
        """
        # TODO: 可将单轮摘要替换为 LLM 生成（每轮只生成一次并缓存）。
        user_text = ""
        assistant_text = ""
        names = tool_call_names(turn)
        tool_records: list[str] = []
        for message in turn:
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str) and not user_text:
                user_text = content
            elif role == "assistant" and isinstance(content, str) and content:
                assistant_text = content
            elif role == "tool":
                tool_records.append(_summarize_tool_message(message, names))
        return {
            "number": number,
            "user": user_text,
            "assistant": assistant_text,
            "tools": tool_records,
        }

    def render(self, archive: HistoryArchive) -> str:
        """把归档条目渲染为不超过上限的摘要。

        超限时永久丢弃最早条目；只剩一条仍超限时，按比例压缩这一条的各字段后
        重新渲染，保证模板结构（含闭合标签）完整。

        Args:
            archive: 历史归档，可能被就地裁剪。

        Returns:
            摘要文本；没有归档时为空字符串。
        """
        if not archive.entries and not archive.omitted_count:
            return ""
        while True:
            summary = self._render(archive)
            if self.estimator.estimate(summary) <= self.max_tokens:
                return summary
            if len(archive.entries) <= 1:
                break
            archive.entries.pop(0)
            archive.omitted_count += 1
        max_chars = int(self.max_tokens * self.estimator.chars_per_token)
        archive.entries[0] = _shrink_entry(archive.entries[0], max_chars)
        return self._render(archive)

    def _render(self, archive: HistoryArchive) -> str:
        """用模板渲染当前归档。

        Args:
            archive: 历史归档。

        Returns:
            摘要文本。
        """
        return self.renderer.render(
            _TEMPLATE,
            omitted_count=archive.omitted_count,
            turns=archive.entries,
        )


def _shrink_entry(entry: Mapping[str, Any], max_chars: int) -> dict[str, Any]:
    """按比例压缩单条摘要：用户 1/4、助手 1/2、工具记录 1/4。

    Args:
        entry: 摘要条目（number、user、assistant、tools）。
        max_chars: 整条允许的大致字符数。

    Returns:
        压缩后的新条目；工具记录超出时保留前几条并注明省略条数。
    """
    tools_budget = max_chars // 4
    tools: list[str] = []
    used = 0
    for record in entry["tools"]:
        line = oneline(record)
        if used + len(line) > tools_budget:
            break
        tools.append(line)
        used += len(line)
    if len(tools) < len(entry["tools"]):
        tools.append(f"另有 {len(entry['tools']) - len(tools)} 条工具记录已省略")
    return {
        **entry,
        "user": truncate_middle(oneline(entry["user"]), max_chars // 4),
        "assistant": truncate_middle(oneline(entry["assistant"]), max_chars // 2),
        "tools": tools,
    }


def _summarize_tool_message(
    message: Mapping[str, Any],
    tool_names: Mapping[str, str],
) -> str:
    """将工具消息提取为不含大段正文的事实记录。

    Args:
        message: 需要提取的 ``role=tool`` 消息。
        tool_names: 工具调用 ID 到工具名称的映射。

    Returns:
        包含工具名、结果、关键定位字段和操作记录的单行文本。
    """
    tool_name = tool_names.get(message.get("tool_call_id"), "unknown_tool")
    content = message.get("content")
    if not isinstance(content, str):
        return f"{tool_name} 返回了非文本结果"
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return f"{tool_name}: {_truncate(content, 500)}"
    if not isinstance(result, Mapping):
        return f"{tool_name}: {_truncate(content, 500)}"

    status = "失败" if result.get("ok") is False else "成功"
    fields = [f"{tool_name} {status}"]
    for key in ("path", "source_path", "revision", "previous_revision"):
        value = result.get(key)
        if isinstance(value, str) and value:
            fields.append(f"{key}={value}")
    error = result.get("error")
    if isinstance(error, str) and error:
        fields.append(f"error={_truncate(error, 300)}")
    block_ids = result_block_ids(result)
    if block_ids:
        fields.append(f"范围={block_ids[0]} ~ {block_ids[-1]}（{len(block_ids)} 块）")
    if result.get("view") == "outline":
        fields.append("视图=大纲")
    operations = _summarize_operations(result)
    if operations:
        fields.append(f"操作={_truncate(operations, 500)}")
    return ", ".join(fields)


def _summarize_operations(result: Mapping[str, Any]) -> str:
    """把结果中的逐项操作记录压成一行，保留修改和记录类工具做了什么。

    识别 ``operations`` 或 ``results`` 数组中含 ``op`` 的条目，
    拼接其 op、目标 ID（id 或 finding_id）和结果说明（message）。

    Args:
        result: 工具返回结果对象。

    Returns:
        以分号分隔的操作记录；没有可识别的操作时为空字符串。
    """
    items: list[str] = []
    for key in ("operations", "results"):
        entries = result.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("op"), str):
                continue
            parts = [
                str(entry[field])
                for field in ("op", "id", "finding_id", "message")
                if isinstance(entry.get(field), (str, int)) and str(entry.get(field))
            ]
            items.append(" ".join(parts))
    return "; ".join(items)


def _truncate(text: str, max_chars: int) -> str:
    """将文本限制在指定字符数内并显式标记截断。

    Args:
        text: 需要限制长度的原始文本。
        max_chars: 允许的最大字符数。

    Returns:
        未超限的原文，或带截断标记的文本。
    """
    if len(text) <= max_chars:
        return text
    if max_chars <= len(_TRUNCATED_MARKER):
        return _TRUNCATED_MARKER[:max_chars]
    return f"{text[: max_chars - len(_TRUNCATED_MARKER)]}{_TRUNCATED_MARKER}"
