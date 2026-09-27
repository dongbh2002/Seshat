"""对照组压缩策略：常见的简单做法，与 TieredCompressionStrategy 使用同一接口，便于公平对比。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from backend.runtime.context.compression import (
    CompressionResult,
    CompressionStats,
    CompressionStrategy,
    Context,
    HistoryArchive,
    group_completed_turns,
)
from backend.runtime.context.token_estimator import TokenEstimator

_CLEARED_TOOL_RESULT = json.dumps(  # 清理后的工具结果占位。
    {"cleared": True, "note": "旧工具结果已清理，需要时请重新调用工具。"},
    ensure_ascii=False,
)


class _BaselineStrategy(CompressionStrategy):
    """对照组公共部分：按轮分组、估算总量、按需丢弃最旧轮次。"""

    def __init__(self, working_tokens: int, estimator: TokenEstimator) -> None:
        """初始化对照组策略。

        Args:
            working_tokens: 工作上下文预算。
            estimator: 与被测系统共用的 token 估算器。

        Returns:
            None。
        """
        self.working_tokens = working_tokens  # 工作上下文预算。
        self.estimator = estimator  # token 估算器。

    def _result(
        self,
        history: Context,
        active_turn: Context,
        archive: HistoryArchive,
        tokens_before: int,
        reserved_tokens: int,
    ) -> CompressionResult:
        """组装对照组的压缩结果；对照组不归档、不维护摘要。

        Args:
            history: 压缩后的历史消息。
            active_turn: 当前轮次消息。
            archive: 原样返回的历史归档。
            tokens_before: 压缩前估算。
            reserved_tokens: 指令等预留占用。

        Returns:
            压缩结果。
        """
        stats = CompressionStats(
            tokens_before=tokens_before,
            tokens_after=reserved_tokens
            + self.estimator.estimate_messages([*history, *active_turn]),
        )
        return CompressionResult(
            history=history,
            active_turn=active_turn,
            history_summary="",
            archive=archive,
            archived_message_count=0,
            stats=stats,
        )

    def _drop_oldest_turns(
        self,
        history: Context,
        active_turn: Context,
        reserved_tokens: int,
    ) -> Context:
        """超出预算时从最旧开始整轮丢弃历史，当前轮不动。

        Args:
            history: 历史消息副本。
            active_turn: 当前轮次消息。
            reserved_tokens: 指令等预留占用。

        Returns:
            丢弃后的历史消息。
        """
        turns = group_completed_turns(history)
        active_tokens = self.estimator.estimate_messages(active_turn)
        sizes = [self.estimator.estimate_messages(turn) for turn in turns]
        while (
            turns and reserved_tokens + active_tokens + sum(sizes) > self.working_tokens
        ):
            turns.pop(0)
            sizes.pop(0)
        return [message for turn in turns for message in turn]


class NoCompression(_BaselineStrategy):
    """不做任何压缩：历史原样保留。"""

    def compress(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
        *,
        archive: HistoryArchive,
        reserved_tokens: int,
    ) -> CompressionResult:
        """原样返回全部消息。

        Args:
            history: 历史消息。
            active_turn: 当前轮次消息。
            archive: 历史归档。
            reserved_tokens: 指令等预留占用。

        Returns:
            未压缩的结果。
        """
        messages = [dict(message) for message in history]
        active = [dict(message) for message in active_turn]
        before = reserved_tokens + self.estimator.estimate_messages(
            [*messages, *active]
        )
        return self._result(messages, active, archive, before, reserved_tokens)


class SlidingWindow(_BaselineStrategy):
    """滑动窗口：超出预算时从最旧开始整轮丢弃历史。"""

    def compress(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
        *,
        archive: HistoryArchive,
        reserved_tokens: int,
    ) -> CompressionResult:
        """丢弃最旧的完整轮次直到不超预算。

        Args:
            history: 历史消息。
            active_turn: 当前轮次消息。
            archive: 历史归档。
            reserved_tokens: 指令等预留占用。

        Returns:
            压缩结果。
        """
        messages = [dict(message) for message in history]
        active = [dict(message) for message in active_turn]
        before = reserved_tokens + self.estimator.estimate_messages(
            [*messages, *active]
        )
        kept = self._drop_oldest_turns(messages, active, reserved_tokens)
        return self._result(kept, active, archive, before, reserved_tokens)


class ClearToolResults(_BaselineStrategy):
    """清理旧工具结果：已结束轮次的工具结果一律替换为占位，仍超预算再滑动窗口。"""

    def compress(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
        *,
        archive: HistoryArchive,
        reserved_tokens: int,
    ) -> CompressionResult:
        """先清理历史中的工具结果，仍超预算时整轮丢弃最旧历史。

        Args:
            history: 历史消息。
            active_turn: 当前轮次消息。
            archive: 历史归档。
            reserved_tokens: 指令等预留占用。

        Returns:
            压缩结果。
        """
        messages = [dict(message) for message in history]
        active = [dict(message) for message in active_turn]
        before = reserved_tokens + self.estimator.estimate_messages(
            [*messages, *active]
        )
        cleared = [
            {**message, "content": _CLEARED_TOOL_RESULT}
            if message.get("role") == "tool"
            else message
            for message in messages
        ]
        kept = self._drop_oldest_turns(cleared, active, reserved_tokens)
        return self._result(kept, active, archive, before, reserved_tokens)
