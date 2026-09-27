"""评估指标：从每次发给模型的请求中测量成本、信息可用性、正确性与缓存友好度。

所有判断都以磁盘上文档的真实当前版本为准（每步重新解析），不依赖被测策略自身的记录。
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from backend.utils.docx import DocumentBlock, DocxParser, calculate_revision


class DocumentTruth:
    """文档当前版本各块的 Markdown（accepted 视图），文件变化时自动重新解析。"""

    def __init__(self, path: Path) -> None:
        """初始化并解析一次。

        Args:
            path: 评估用文档副本的路径。

        Returns:
            None。
        """
        self.path = path  # 文档副本路径。
        self.revision = ""  # 最近解析时的 revision。
        self.blocks: dict[str, DocumentBlock] = {}  # 块 ID 到当前版本内容块。
        self.refresh()

    def refresh(self) -> None:
        """文件内容变化时重新解析。

        Returns:
            None。
        """
        revision = calculate_revision(self.path)
        if revision == self.revision:
            return
        with ZipFile(self.path) as archive:
            blocks = DocxParser(archive).parse_blocks("accepted")
        self.blocks = {block.block_id: block for block in blocks}
        self.revision = revision

    def markdown(self, block_id: str) -> str | None:
        """返回块当前版本的 Markdown；块不存在时为 None。

        Args:
            block_id: 块 ID。

        Returns:
            Markdown 或 None。
        """
        block = self.blocks.get(block_id)
        return block.to_markdown() if block is not None else None

    def content_ids(self, block_ids: Sequence[str]) -> list[str]:
        """筛出正文非空的块（修订删除后正文为空的块不算“需要的内容”）。

        Args:
            block_ids: 候选块 ID。

        Returns:
            正文非空的块 ID。
        """
        return [
            block_id
            for block_id in block_ids
            if block_id in self.blocks and self.blocks[block_id].text.strip()
        ]


def request_chars(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> int:
    """按与 token 校准相同的口径计算一次请求的字符数。

    Args:
        messages: 请求消息。
        tools: 工具定义。

    Returns:
        字符数。
    """
    return sum(_compact_len(dict(message)) for message in messages) + (
        _compact_len(list(tools)) if tools else 0
    )


def serialize_request(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> str:
    """把请求序列化为“工具定义在前、消息在后”的字符串，用于比较前缀。

    Args:
        messages: 请求消息。
        tools: 工具定义。

    Returns:
        序列化字符串。
    """
    return json.dumps(
        {"tools": list(tools), "messages": [dict(m) for m in messages]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def prefix_reuse(previous: str | None, current: str) -> float:
    """当前请求与上一次请求的最长公共前缀占当前请求的比例（前缀缓存的代理指标）。

    Args:
        previous: 上一次请求的序列化字符串；首次请求为 None。
        current: 当前请求的序列化字符串。

    Returns:
        0 到 1 之间的比例；首次请求为 0。
    """
    if not previous or not current:
        return 0.0
    return len(os.path.commonprefix([previous, current])) / len(current)


def visible_blocks(messages: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """提取请求中所有读取结果里仍可见的块正文（按 index 的位置切出）。

    Args:
        messages: 请求消息。

    Returns:
        ``(块 ID, 块 Markdown)`` 列表；同一块出现多次会有多项。
    """
    visible: list[tuple[str, str]] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            result = json.loads(str(message.get("content")))
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict) or not isinstance(result.get("markdown"), str):
            continue
        markdown = result["markdown"]
        for item in result.get("index", []):
            if (
                isinstance(item, dict)
                and not item.get("cleaned")
                and isinstance(item.get("offset"), int)
            ):
                start = item["offset"]
                visible.append((item["id"], markdown[start : start + item["length"]]))
    return visible


def available(
    visible: Sequence[tuple[str, str]],
    truth: DocumentTruth,
    block_ids: Sequence[str],
) -> bool:
    """判断所需块的当前版本正文是否都在上下文中（过时版本不算）。

    Args:
        visible: 请求中可见的块正文。
        truth: 文档当前版本。
        block_ids: 所需块 ID。

    Returns:
        全部可用时为 True。
    """
    pairs = set(visible)
    return all((block_id, truth.markdown(block_id)) in pairs for block_id in block_ids)


def has_stale_copy(
    visible: Sequence[tuple[str, str]],
    truth: DocumentTruth,
    block_ids: Sequence[str],
) -> bool:
    """判断上下文中是否有所需块的过时副本（模型可能被旧内容误导）。

    Args:
        visible: 请求中可见的块正文。
        truth: 文档当前版本。
        block_ids: 所需块 ID。

    Returns:
        存在过时副本时为 True。
    """
    needed = set(block_ids)
    return any(
        block_id in needed and text != truth.markdown(block_id)
        for block_id, text in visible
    )


def stale_block_count(visible: Sequence[tuple[str, str]], truth: DocumentTruth) -> int:
    """统计上下文中与当前版本不一致的块数（模型会看到旧内容）。

    Args:
        visible: 请求中可见的块正文。
        truth: 文档当前版本。

    Returns:
        过时块的数量。
    """
    return sum(
        1
        for block_id, text in visible
        if block_id in truth.blocks and text != truth.markdown(block_id)
    )


def duplicate_chars(visible: Sequence[tuple[str, str]]) -> int:
    """统计重复出现的块正文占用的字符数（同一块正文出现 n 次，计 n-1 份）。

    Args:
        visible: 请求中可见的块正文。

    Returns:
        重复部分的字符数。
    """
    counts = Counter(visible)
    return sum(len(text) * (count - 1) for (_, text), count in counts.items())


@dataclass
class StepRecord:
    """一次模型请求的测量结果。"""

    turn: str  # 所属轮次标签。
    prompt_tokens: int  # 按实测比例折算的真实输入 token。
    stale_blocks: int  # 上下文中过时的块数。
    duplicate_tokens: int  # 重复块正文占用的 token。
    constraint_present: bool | None  # 用户约束是否仍在上下文中；约束提出前为 None。
    prefix_reuse: float  # 与上一请求的公共前缀比例。
    compress_ms: float  # 本步压缩耗时（毫秒）。
    report: dict[str, Any] = field(default_factory=dict)  # 被测系统的压缩统计。


@dataclass
class RunTrace:
    """一次评估运行（一种策略 × 一种行为 × 一个预算）的全部测量。"""

    steps: list[StepRecord] = field(default_factory=list)  # 每次请求的测量。
    needs: list[tuple[str, bool]] = field(
        default_factory=list
    )  # 轮次开始时所需内容是否已在上下文。
    answers: list[tuple[str, bool]] = field(
        default_factory=list
    )  # 本轮作答依据是否正确。
    reread_tokens: int = 0  # 因所需内容缺失而补读所花的 token。
    reread_count: int = 0  # 补读的工具调用次数。
    recall: float | None = None  # 最后一轮时早先用户请求的保留比例。
    history_messages: int = 0  # 运行结束时 AgentLoop 保存的历史消息条数（内存占用）。
    edit_applied: bool = False  # 编辑轮次是否成功写入文档。
    error: str | None = None  # 运行中断时的错误信息。


def summarize_trace(trace: RunTrace, working_tokens: int) -> dict[str, Any]:
    """把一次运行的测量汇总为报告指标。

    Args:
        trace: 一次运行的测量。
        working_tokens: 本次运行的工作预算。

    Returns:
        指标字典，键含义见报告中的指标定义。
    """
    steps = trace.steps
    prompts = [step.prompt_tokens for step in steps]
    constraint = [
        s.constraint_present for s in steps if s.constraint_present is not None
    ]
    reused = [s.prefix_reuse for s in steps[1:]]
    reports = [s.report for s in steps if s.report]
    return {
        "steps": len(steps),
        "total_prompt_tokens": sum(prompts),
        "uncached_tokens_est": sum(
            round(step.prompt_tokens * (1 - step.prefix_reuse)) for step in steps
        ),
        "peak_prompt_tokens": max(prompts, default=0),
        "over_budget_steps": sum(1 for p in prompts if p > working_tokens),
        "need_checks": len(trace.needs),
        "need_hit_rate": _ratio(sum(hit for _, hit in trace.needs), len(trace.needs)),
        "answer_checks": len(trace.answers),
        "answer_grounded_rate": _ratio(
            sum(ok for _, ok in trace.answers), len(trace.answers)
        ),
        "reread_count": trace.reread_count,
        "reread_tokens": trace.reread_tokens,
        "stale_exposure_steps": sum(1 for s in steps if s.stale_blocks),
        "duplicate_tokens_mean": _mean([s.duplicate_tokens for s in steps]),
        "constraint_retention": _ratio(sum(constraint), len(constraint)),
        "recall": trace.recall,
        "prefix_reuse_mean": _mean(reused),
        "compress_ms_mean": _mean([s.compress_ms for s in steps]),
        "history_messages": trace.history_messages,
        "edit_applied": trace.edit_applied,
        "forced_warning_steps": sum(1 for r in reports if r.get("forced_reads")),
        "l1_steps": sum(1 for r in reports if r.get("read_cleanup_triggered")),
        "l2_steps": sum(1 for r in reports if r.get("turn_compaction_triggered")),
        "l3_steps": sum(1 for r in reports if r.get("budget_enforced")),
        "error": trace.error,
    }


def _compact_len(value: Any) -> int:
    """返回对象紧凑 JSON 序列化后的长度。

    Args:
        value: 任意可序列化对象。

    Returns:
        字符数。
    """
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _ratio(numerator: float, denominator: int) -> float | None:
    """安全求比例。

    Args:
        numerator: 分子。
        denominator: 分母。

    Returns:
        比例；分母为 0 时为 None。
    """
    return numerator / denominator if denominator else None


def _mean(values: Sequence[float]) -> float:
    """求平均值。

    Args:
        values: 数值序列。

    Returns:
        平均值；空序列为 0。
    """
    return sum(values) / len(values) if values else 0.0
