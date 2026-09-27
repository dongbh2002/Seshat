"""分级压缩策略（按 token 预算）：只负责各级的触发、顺序与停止条件。

阈值均为 working_tokens 的比例，每一步依次执行：
0. 无损清理（不看阈值）：读取后被修改的块（stale）、之后有更新副本的块（superseded）。
1. 第一级：超过读取清理触发线后，从最旧的读取开始按块清理已审阅的正文，以及
   最近 retain_recent_turn_reads 轮之前、已结束轮次中的正文，一次降到目标线。
2. 第二级：超过轮次归档触发线后，从最旧轮次开始归档为摘要，一次降到目标线；
   至少保留 keep_recent_turns 轮，含未记录审阅读取的轮次受保护。归档不可逆。
3. 第三级：超过 working_tokens 时按价值从低到高舍弃：先丢早期摘要，再清理历史中
   仍保留的读取正文（先保留有 open 问题的块，仍超再清），再逐轮归档剩余历史，
   再丢这些摘要；当前轮不参与。
4. 兜底：仍超预算时清理当前轮中模型已看过的读取，并警告。

压缩结果由调用方写回，下一步在其上继续：清理到目标线后，上下文要重新涨到
触发线才会再次清理，已清理的正文不会恢复。按块清理见 read_results，摘要见 summary。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.config import CompressionSettings
from backend.runtime.context.compression.base import (
    CompressionResult,
    CompressionStats,
    CompressionStrategy,
    Context,
    HistoryArchive,
    group_completed_turns,
    tool_call_names,
)
from backend.runtime.context.compression.read_results import (
    clean_blocks,
    describe_read,
    find_reads,
    parse_read,
    remaining_ids,
)
from backend.runtime.context.compression.summary import HistorySummarizer
from backend.runtime.context.token_estimator import TokenEstimator
from backend.session import ReviewStateStore
from backend.templating import PromptRenderer


@dataclass
class _Workspace:
    """一次压缩过程中的消息副本及其 token 估算。"""

    messages: Context  # 历史与当前轮次消息的副本，历史在前。
    sizes: list[int]  # 与 messages 对齐的单条 token 估算。
    history_length: int  # messages 中属于历史的条数。
    reserved_tokens: int  # 指令、审阅状态和工具定义占用的 token。
    summary_tokens: int  # 当前历史摘要占用的 token。
    stats: CompressionStats  # 本次压缩的统计，边执行边累计。

    def total(self) -> int:
        """返回当前上下文的 token 估算总量。

        Returns:
            预留、摘要与全部消息之和。
        """
        return self.reserved_tokens + self.summary_tokens + sum(self.sizes)


class TieredCompressionStrategy(CompressionStrategy):
    """按 token 预算分级压缩，清理前检查结论是否已记录，否则警告。"""

    def __init__(
        self,
        settings: CompressionSettings,
        working_tokens: int,
        estimator: TokenEstimator,
        review_state: ReviewStateStore,
        renderer: PromptRenderer,
    ) -> None:
        """初始化分级压缩策略。

        Args:
            settings: YAML 中的压缩配置（比例均相对 working_tokens）。
            working_tokens: 工作上下文软预算。
            estimator: token 估算器。
            review_state: 判断块是否已审阅、是否过时的状态存储。
            renderer: 渲染历史摘要模板的渲染器。

        Returns:
            None。
        """
        self.working_tokens = working_tokens  # 软预算，第三级与兜底的上限。
        self.read_trigger_tokens = int(  # 第一级触发线。
            working_tokens * settings.read_cleanup_trigger_ratio
        )
        self.read_target_tokens = int(  # 第一级目标线。
            working_tokens * settings.read_cleanup_target_ratio
        )
        self.turn_trigger_tokens = int(  # 第二级触发线。
            working_tokens * settings.turn_compaction_trigger_ratio
        )
        self.turn_target_tokens = int(  # 第二级目标线。
            working_tokens * settings.turn_compaction_target_ratio
        )
        self.keep_recent_turns = settings.keep_recent_turns  # 第二级至少保留的轮次。
        self.retain_recent_turn_reads = (  # 第一级不按轮次结束清理的最近轮数。
            settings.retain_recent_turn_reads
        )
        self.cleanup_after_turn_end = (  # 已结束轮次的未标记读取是否可清理。
            settings.cleanup_after_turn_end
        )
        self.estimator = estimator  # token 估算器。
        self.review_state = review_state  # 审阅与版本状态。
        self.summarizer = HistorySummarizer(  # 历史摘要的提取与渲染。
            renderer,
            estimator,
            int(working_tokens * settings.summary_max_ratio),
        )

    def compress(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
        *,
        archive: HistoryArchive,
        reserved_tokens: int,
    ) -> CompressionResult:
        """依次做无损清理、按块清理读取、归档轮次、按价值舍弃，最后兜底当前轮。

        Args:
            history: 已完成轮次的消息（上一步压缩后的结果），不含 system 消息。
            active_turn: 当前轮次消息（上一步压缩后的结果加本步新增）。
            archive: 之前已归档的轮次。
            reserved_tokens: 指令、审阅状态和工具定义已占用的 token。

        Returns:
            压缩后的消息副本（调用方写回）、摘要、更新后的归档与需要提醒的读取信息。
        """
        messages = [dict(message) for message in [*history, *active_turn]]
        archive = HistoryArchive(
            entries=list(archive.entries),
            omitted_count=archive.omitted_count,
            next_number=archive.next_number,
        )
        workspace = _Workspace(
            messages=messages,
            sizes=[self.estimator.estimate(message) for message in messages],
            history_length=len(history),
            reserved_tokens=reserved_tokens,
            summary_tokens=self.estimator.estimate(self.summarizer.render(archive)),
            stats=CompressionStats(),
        )
        workspace.stats.tokens_before = workspace.total()
        omitted_before = archive.omitted_count
        number_before = archive.next_number

        self._clean_stale_blocks(workspace)
        self._clean_superseded_blocks(workspace)
        self._clean_reads_to_target(workspace)
        archived_count = self._archive_turns(workspace, archive)
        enforced_count, forced_reads = self._enforce_working_budget(workspace, archive)
        archived_count += enforced_count
        forced_reads.extend(self._force_clean_active_turn(workspace))
        workspace.stats.tokens_after = workspace.total()
        workspace.stats.archived_turns = archive.next_number - number_before
        workspace.stats.dropped_summary_entries = archive.omitted_count - omitted_before
        return CompressionResult(
            history=workspace.messages[: workspace.history_length],
            active_turn=workspace.messages[workspace.history_length :],
            history_summary=self.summarizer.render(archive),
            archive=archive,
            archived_message_count=archived_count,
            unrecorded_reads=(
                self._collect_unrecorded_reads(workspace)
                if workspace.total() > self.read_trigger_tokens
                else []
            ),
            forced_reads=forced_reads,
            stats=workspace.stats,
        )

    # ---- 第 0 步与第一级：按块清理读取正文 ----

    def _clean_stale_blocks(self, workspace: _Workspace) -> None:
        """清理读取之后被修改过的块，不看阈值。

        Args:
            workspace: 当前压缩过程的消息副本。

        Returns:
            None。
        """
        for index, result in self._seen_reads(workspace):
            stale = self.review_state.get_stale_blocks(
                str(result.get("path")),
                str(result.get("revision")),
                remaining_ids(result),
            )
            if stale:
                self._apply_cleanup(
                    workspace, index, result, dict.fromkeys(stale, "stale")
                )

    def _clean_superseded_blocks(self, workspace: _Workspace) -> None:
        """清理之后被重新读取过的块的旧副本，不看阈值，同一块只保留最新副本。

        同一文档、同一修订视图下，较新的正文读取可替代较旧读取中的相同块；
        大纲读取只替代较旧大纲读取中的块，不替代正文。

        Args:
            workspace: 当前压缩过程的消息副本。

        Returns:
            None。
        """
        covered_by_content: set[tuple[str, str, str]] = set()
        covered_by_any: set[tuple[str, str, str]] = set()
        all_reads = find_reads(workspace.messages, len(workspace.messages))
        for index, result in reversed(all_reads):
            path, mode = str(result.get("path")), str(result.get("mode"))
            is_outline = result.get("view") == "outline"
            removals: dict[str, str] = {}
            for block_id in remaining_ids(result):
                key = (path, mode, block_id)
                if key in (covered_by_any if is_outline else covered_by_content):
                    removals[block_id] = "superseded"
                    continue
                covered_by_any.add(key)
                if not is_outline:
                    covered_by_content.add(key)
            if removals:
                self._apply_cleanup(workspace, index, result, removals)

    def _clean_reads_to_target(self, workspace: _Workspace) -> None:
        """超过第一级触发线时，从最旧的读取开始按块清理可清理的正文。

        可清理：outline 视图、已标记审阅的块，以及开关打开时、最近
        retain_recent_turn_reads 轮之前已结束轮次中的块。降到第一级目标线即停止，
        使清理成批发生、减少前缀缓存失效。

        Args:
            workspace: 当前压缩过程的消息副本。

        Returns:
            None。
        """
        if workspace.total() <= self.read_trigger_tokens:
            return
        workspace.stats.read_cleanup_triggered = True
        retained_start = self._recent_turns_start(workspace)
        for index, result in self._seen_reads(workspace):
            removals = self._cleanable_blocks(index, result, retained_start)
            if removals:
                self._apply_cleanup(workspace, index, result, removals)
            if workspace.total() <= self.read_target_tokens:
                return

    def _cleanable_blocks(
        self,
        index: int,
        result: Mapping[str, Any],
        retained_start: int,
    ) -> dict[str, str]:
        """返回一次读取中当前可以清理的块及原因。

        Args:
            index: 读取结果所在的消息下标。
            result: 已解析的读取结果。
            retained_start: 最近几轮保留读取的起始下标，之前的已结束轮次可清理。

        Returns:
            块 ID 到清理原因的映射。
        """
        remaining = remaining_ids(result)
        if result.get("view") == "outline":
            return dict.fromkeys(remaining, "reviewed")
        unreviewed = set(
            self.review_state.get_unreviewed(str(result.get("path")), remaining)
        )
        turn_ended = self.cleanup_after_turn_end and index < retained_start
        removals: dict[str, str] = {}
        for block_id in remaining:
            if block_id not in unreviewed:
                removals[block_id] = "reviewed"
            elif turn_ended:
                removals[block_id] = "turn_ended"
        return removals

    def _recent_turns_start(self, workspace: _Workspace) -> int:
        """返回最近 retain_recent_turn_reads 个已结束轮次在消息中的起始下标。

        Args:
            workspace: 当前压缩过程的消息副本。

        Returns:
            起始下标；保留轮数为 0 时为历史长度。
        """
        turns = group_completed_turns(workspace.messages[: workspace.history_length])
        retained = (
            turns[max(len(turns) - self.retain_recent_turn_reads, 0) :]
            if self.retain_recent_turn_reads
            else []
        )
        return workspace.history_length - sum(len(turn) for turn in retained)

    def _collect_unrecorded_reads(self, workspace: _Workspace) -> list[dict[str, Any]]:
        """列出仍留有未审阅、且按规则不能清理的正文的读取，供上下文提醒。

        Args:
            workspace: 压缩完成后的消息副本。

        Returns:
            未记录读取的范围描述列表。
        """
        unrecorded: list[dict[str, Any]] = []
        for index, result in self._seen_reads(workspace):
            if result.get("view") == "outline":
                continue
            if self.cleanup_after_turn_end and index < workspace.history_length:
                continue
            unreviewed = self.review_state.get_unreviewed(
                str(result.get("path")),
                remaining_ids(result),
            )
            if unreviewed:
                unrecorded.append(
                    describe_read(workspace.messages[index], result, unreviewed)
                )
        return unrecorded

    # ---- 第二、三级：归档轮次与按价值舍弃 ----

    def _archive_turns(self, workspace: _Workspace, archive: HistoryArchive) -> int:
        """第二级：超过触发线时从最旧轮次开始归档，降到目标线即停。

        至少保留 keep_recent_turns 轮；遇到含未记录审阅读取的轮次停止。

        Args:
            workspace: 当前压缩过程的消息副本，归档的消息从头部移除。
            archive: 历史归档，就地追加条目。

        Returns:
            本次归档的消息条数。
        """
        if workspace.total() <= self.turn_trigger_tokens:
            return 0
        workspace.stats.turn_compaction_triggered = True
        turn_lengths = self._history_turn_lengths(workspace)
        archived_count = 0
        while (
            len(turn_lengths) > self.keep_recent_turns
            and workspace.total() > self.turn_target_tokens
        ):
            if self._find_unrecorded_reads(workspace.messages[: turn_lengths[0]]):
                break
            archived_count += self._archive_oldest_turn(
                workspace, archive, turn_lengths.pop(0)
            )
        return archived_count

    def _enforce_working_budget(
        self,
        workspace: _Workspace,
        archive: HistoryArchive,
    ) -> tuple[int, list[dict[str, Any]]]:
        """第三级：超过 working_tokens 时按价值从低到高舍弃，当前轮不参与。

        依次：丢弃已有摘要条目（最旧优先）→ 清理历史中仍保留的读取正文（先保留
        有 open 问题的块，仍超再清）→ 逐轮归档剩余历史 → 丢弃这些摘要；每一步后
        重新计算，降到预算内即停止。文档原文可按块重读、对话原文不可恢复，因此先
        舍弃正文。未记录审阅即被清理或归档的读取作为强制清理返回，由上下文给出警告。

        Args:
            workspace: 当前压缩过程的消息副本。
            archive: 历史归档，就地修改。

        Returns:
            本阶段归档的消息条数，以及被强制归档的未记录读取。
        """
        archived_count = 0
        forced_reads: list[dict[str, Any]] = []
        if workspace.total() > self.working_tokens:
            workspace.stats.budget_enforced = True
        while workspace.total() > self.working_tokens and archive.entries:
            self._drop_oldest_summary_entry(workspace, archive)
        for keep_open_findings in (True, False):
            if workspace.total() <= self.working_tokens:
                break
            forced_reads.extend(
                self._clean_history_reads(
                    workspace, keep_open_findings=keep_open_findings
                )
            )
        turn_lengths = self._history_turn_lengths(workspace)
        while workspace.total() > self.working_tokens and turn_lengths:
            length = turn_lengths.pop(0)
            forced_reads.extend(
                self._find_unrecorded_reads(workspace.messages[:length])
            )
            archived_count += self._archive_oldest_turn(workspace, archive, length)
        while workspace.total() > self.working_tokens and archive.entries:
            self._drop_oldest_summary_entry(workspace, archive)
        return archived_count, forced_reads

    def _clean_history_reads(
        self,
        workspace: _Workspace,
        *,
        keep_open_findings: bool,
    ) -> list[dict[str, Any]]:
        """从最旧开始清理历史中仍保留的读取正文，降到 working_tokens 即停。

        已审阅的块标为 reviewed；未审阅的块在 cleanup_after_turn_end 打开时标为
        turn_ended，否则标为 forced 并返回警告信息。

        Args:
            workspace: 当前压缩过程的消息副本。
            keep_open_findings: 是否保留有 open 问题的块（最可能马上被修改）。

        Returns:
            被强制清理且含未记录块的读取范围列表。
        """
        forced_reads: list[dict[str, Any]] = []
        for index, result in self._seen_reads(workspace):
            if index >= workspace.history_length:
                break
            path = str(result.get("path"))
            kept = (
                self.review_state.get_open_finding_blocks(path)
                if keep_open_findings
                else set()
            )
            targets = [
                block_id for block_id in remaining_ids(result) if block_id not in kept
            ]
            if not targets:
                continue
            unreviewed = set(self.review_state.get_unreviewed(path, targets))
            fallback = "turn_ended" if self.cleanup_after_turn_end else "forced"
            removals = {
                block_id: fallback if block_id in unreviewed else "reviewed"
                for block_id in targets
            }
            forced = [
                block_id for block_id in targets if removals[block_id] == "forced"
            ]
            if forced:
                forced_reads.append(
                    describe_read(workspace.messages[index], result, forced)
                )
            self._apply_cleanup(workspace, index, result, removals)
            if workspace.total() <= self.working_tokens:
                break
        return forced_reads

    def _history_turn_lengths(self, workspace: _Workspace) -> list[int]:
        """返回历史中每个已结束轮次的消息条数，按时间排序。

        Args:
            workspace: 当前压缩过程的消息副本。

        Returns:
            各轮消息条数。
        """
        return [
            len(turn)
            for turn in group_completed_turns(
                workspace.messages[: workspace.history_length]
            )
        ]

    def _drop_oldest_summary_entry(
        self,
        workspace: _Workspace,
        archive: HistoryArchive,
    ) -> None:
        """永久丢弃最早的一条摘要条目，并更新摘要占用。

        Args:
            workspace: 当前压缩过程的消息副本。
            archive: 历史归档。

        Returns:
            None。
        """
        archive.entries.pop(0)
        archive.omitted_count += 1
        workspace.summary_tokens = self.estimator.estimate(
            self.summarizer.render(archive)
        )

    def _archive_oldest_turn(
        self,
        workspace: _Workspace,
        archive: HistoryArchive,
        length: int,
    ) -> int:
        """把历史最前面的一轮转为摘要条目，并从消息副本中移除。

        Args:
            workspace: 当前压缩过程的消息副本。
            archive: 历史归档。
            length: 该轮的消息条数。

        Returns:
            被移除的消息条数。
        """
        archive.entries.append(
            self.summarizer.summarize_turn(
                archive.next_number, workspace.messages[:length]
            )
        )
        archive.next_number += 1
        del workspace.messages[:length]
        del workspace.sizes[:length]
        workspace.history_length -= length
        workspace.summary_tokens = self.estimator.estimate(
            self.summarizer.render(archive)
        )
        return length

    def _find_unrecorded_reads(self, turn: Context) -> list[dict[str, Any]]:
        """找出一个已完成轮次中留有未审阅正文、且按规则不能直接丢弃的读取。

        开关 cleanup_after_turn_end 打开时，已结束轮次的读取视为可丢弃，返回空。

        Args:
            turn: 一个已完成轮次的消息。

        Returns:
            未记录读取的范围描述列表。
        """
        if self.cleanup_after_turn_end:
            return []
        names = tool_call_names(turn)
        unrecorded: list[dict[str, Any]] = []
        for message in turn:
            result = parse_read(message, names)
            if result is None or result.get("view") == "outline":
                continue
            unreviewed = self.review_state.get_unreviewed(
                str(result.get("path")),
                remaining_ids(result),
            )
            if unreviewed:
                unrecorded.append(describe_read(message, result, unreviewed))
        return unrecorded

    # ---- 兜底：当前轮 ----

    def _force_clean_active_turn(self, workspace: _Workspace) -> list[dict[str, Any]]:
        """仍超 working_tokens 时，强制整条清理当前轮次中模型已看到的读取。

        Args:
            workspace: 当前压缩过程的消息副本。

        Returns:
            被强制清理且含未记录块的读取范围列表。
        """
        forced_reads: list[dict[str, Any]] = []
        if workspace.total() <= self.working_tokens:
            return forced_reads
        workspace.stats.active_turn_forced = True
        for index, result in self._seen_reads(workspace):
            if index < workspace.history_length:
                continue
            remaining = remaining_ids(result)
            unreviewed = self.review_state.get_unreviewed(
                str(result.get("path")),
                remaining,
            )
            if unreviewed:
                forced_reads.append(
                    describe_read(workspace.messages[index], result, unreviewed)
                )
            removals = dict.fromkeys(remaining, "reviewed")
            removals.update(dict.fromkeys(unreviewed, "forced"))
            self._apply_cleanup(workspace, index, result, removals)
            if workspace.total() <= self.working_tokens:
                break
        # TODO: 模型尚未看到的单个结果本身超过预算时，需要工具分页限流。
        return forced_reads

    # ---- 工作区操作 ----

    @staticmethod
    def _seen_reads(workspace: _Workspace) -> list[tuple[int, dict[str, Any]]]:
        """列出模型已看到、仍含正文的读取结果，按从旧到新排列。

        模型已看到：历史中的全部结果，以及当前轮最后一条 assistant 之前的结果。

        Args:
            workspace: 当前压缩过程的消息副本。

        Returns:
            ``(消息下标, 已解析结果)`` 列表。
        """
        messages = workspace.messages
        seen_end = next(
            (
                index + 1
                for index in range(len(messages) - 1, workspace.history_length - 1, -1)
                if messages[index].get("role") == "assistant"
            ),
            workspace.history_length,
        )
        return find_reads(messages, seen_end)

    def _apply_cleanup(
        self,
        workspace: _Workspace,
        index: int,
        result: Mapping[str, Any],
        removals: Mapping[str, str],
    ) -> None:
        """按块清理一次读取，替换工作区中的消息并累计统计。

        Args:
            workspace: 当前压缩过程的消息副本，就地替换消息并更新估算。
            index: 读取结果所在的消息下标。
            result: 已解析的读取结果。
            removals: 本次要清理的块 ID 到原因的映射。

        Returns:
            None。
        """
        cleanup = clean_blocks(workspace.messages[index], result, removals)
        if cleanup is None:
            return
        stats = workspace.stats
        for reason, count in cleanup.cleaned.items():
            stats.cleaned_blocks[reason] = stats.cleaned_blocks.get(reason, 0) + count
        if cleanup.compacted:
            stats.compacted_reads += 1
        workspace.messages[index] = cleanup.message
        workspace.sizes[index] = self.estimator.estimate(cleanup.message)
