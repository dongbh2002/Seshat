"""上下文压缩策略基类、共用的数据结构，以及按轮分组等消息工具。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Context = list[dict[str, Any]]


@dataclass
class HistoryArchive:
    """已从原始历史中移出、只以摘要形式保留的轮次。

    归档只追加不回退：轮次一旦归档，原始消息从 AgentLoop 的历史中删除，
    之后只以 entries 渲染的摘要出现，保证摘要文本稳定、利于前缀缓存。
    """

    entries: list[dict[str, Any]]  # 仍在摘要中展示的轮次条目，按时间排序。
    omitted_count: int  # 因摘要上限被丢弃的最早轮次数。
    next_number: int  # 下一个归档轮次的编号，从 1 开始。

    @classmethod
    def empty(cls) -> HistoryArchive:
        """创建空归档。

        Returns:
            没有任何轮次的归档。
        """
        return cls(entries=[], omitted_count=0, next_number=1)


@dataclass
class CompressionStats:
    """一次压缩做了什么，用于日志与调参。

    每一步都在上一步压缩后的消息上继续压缩，因此各项计数都是本步的增量。
    """

    tokens_before: int = 0  # 压缩前估算：预留 + 摘要 + 上一步留下的消息与本步新增消息。
    tokens_after: int = 0  # 压缩后估算，口径同上。
    read_cleanup_triggered: bool = False  # 是否超过第一级触发线。
    turn_compaction_triggered: bool = False  # 是否超过第二级触发线。
    budget_enforced: bool = False  # 是否超过 working_tokens 进入第三级。
    active_turn_forced: bool = False  # 是否强制清理了当前轮的读取。
    cleaned_blocks: dict[str, int] = field(  # 本步清理的块数，按原因统计。
        default_factory=dict
    )
    compacted_reads: int = 0  # 本步整条替换为引用的读取数。
    archived_turns: int = 0  # 本步新归档为摘要的轮数（归档不可逆，是增量）。
    dropped_summary_entries: int = 0  # 本步新丢弃的摘要条数（不可逆，是增量）。


@dataclass
class CompressionResult:
    """一次压缩的输出；history 与 active_turn 由调用方写回，作为下一步的输入。"""

    history: Context  # 压缩后的历史消息（已归档轮次不在其中）。
    active_turn: Context  # 压缩后的当前轮次消息。
    history_summary: str  # 已归档轮次渲染的摘要，没有归档时为空字符串。
    archive: HistoryArchive  # 更新后的历史归档，由调用方保存供下次使用。
    archived_message_count: int  # 本步归档移出的消息条数，仅供统计。
    unrecorded_reads: list[dict[str, Any]] = field(  # 因未记录审阅而无法清理的读取。
        default_factory=list
    )
    forced_reads: list[dict[str, Any]] = field(  # 超预算被强制清理的未记录读取。
        default_factory=list
    )
    stats: CompressionStats = field(  # 本次压缩的统计。
        default_factory=CompressionStats
    )


class CompressionStrategy(ABC):
    """定义历史与当前轮次消息的压缩接口，便于替换不同压缩机制。

    压缩结果会写回调用方、作为下一步的输入，清理与归档因此持续生效，
    触发线与目标线之间的迟滞才有意义；实现须能在自己的输出上重复执行。
    """

    @abstractmethod
    def compress(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
        *,
        archive: HistoryArchive,
        reserved_tokens: int,
    ) -> CompressionResult:
        """压缩历史与当前轮次消息。

        Args:
            history: 已完成轮次的消息（上一步压缩后的结果），不含 system 消息。
            active_turn: 当前轮次消息（上一步压缩后的结果加本步新增）。
            archive: 之前已归档的轮次。
            reserved_tokens: 指令、审阅状态和工具定义已占用的 token 估算值。

        Returns:
            压缩后的消息副本（调用方写回）、摘要、更新后的归档及未能清理的读取信息。
        """
        raise NotImplementedError


def group_completed_turns(messages: Sequence[Mapping[str, Any]]) -> list[Context]:
    """按用户消息边界把已完成的消息分成轮次。

    Args:
        messages: 已完成轮次的消息，按时间排序。

    Returns:
        按时间排序的轮次列表，每轮以用户消息开头（首轮可能不是）。
    """
    turns: list[Context] = []
    for message in messages:
        if message.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(dict(message))
    return turns


def tool_call_names(messages: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """建立工具调用 ID 到工具名称的映射。

    Args:
        messages: 可能包含 assistant tool_calls 的消息。

    Returns:
        工具调用 ID 到 function name 的字典。
    """
    names: dict[str, str] = {}
    for message in messages:
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            call_id = tool_call.get("id")
            function = tool_call.get("function")
            if not isinstance(call_id, str) or not isinstance(function, Mapping):
                continue
            name = function.get("name")
            if isinstance(name, str):
                names[call_id] = name
    return names
