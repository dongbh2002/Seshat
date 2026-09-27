"""审阅状态上下文，把 ReviewStateStore 与文档结构渲染为有体积上限的提示词。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from backend.config import ReviewStateSettings
from backend.session import ReviewStateStore
from backend.templating import PromptRenderer
from backend.utils.docx import DocumentIndex, DocumentSnapshot

_TEMPLATE = "review_state_prompt.j2"


class ReviewStateContext:
    """按配置裁剪大纲与问题列表，生成注入模型上下文的审阅工作状态。"""

    def __init__(
        self,
        settings: ReviewStateSettings,
        store: ReviewStateStore,
        document_index: DocumentIndex,
        renderer: PromptRenderer,
    ) -> None:
        """初始化审阅状态上下文。

        Args:
            settings: 审阅状态展示上限配置。
            store: 跨轮次保留的结构化审阅状态。
            document_index: 为已登记文档提供大纲和章节的结构索引。
            renderer: 提示词模板渲染器。

        Returns:
            None。
        """
        self.outline_max_level = settings.outline_max_level  # 大纲展示的最大标题级别。
        self.max_open_findings = settings.max_open_findings  # 展示的 open 问题上限。
        self.finding_max_chars = settings.finding_max_chars  # 单条问题展示字符上限。
        self.store = store  # 审阅状态存储。
        self.document_index = document_index  # 文档结构索引。
        self.renderer = renderer  # 提示词模板渲染器。

    def render(
        self,
        *,
        unrecorded_reads: Sequence[Mapping[str, Any]],
        forced_reads: Sequence[Mapping[str, Any]],
    ) -> str:
        """渲染审阅工作状态。

        Args:
            unrecorded_reads: 压缩策略返回的未记录审阅读取范围。
            forced_reads: 压缩策略返回的被强制清理读取范围。

        Returns:
            已渲染的审阅状态提示词。
        """
        state = self.store.snapshot()
        documents: list[dict[str, Any]] = []
        for path, document in state["documents"].items():
            try:
                snapshot = self.document_index.load(path)
            except (OSError, ValueError) as error:
                documents.append({"path": path, "error": str(error)})
                continue
            documents.append(
                self._build_document_view(
                    snapshot,
                    read_revision=document["revision"],
                    reviewed=set(document["reviewed_block_ids"]),
                )
            )

        open_findings = [
            finding for finding in state["findings"] if finding["status"] == "open"
        ]
        shown_findings = [
            {**finding, "issue": self._truncate(finding["issue"])}
            for finding in open_findings[-self.max_open_findings :]
        ]
        closed_counts = Counter(
            finding["status"]
            for finding in state["findings"]
            if finding["status"] != "open"
        )
        # TODO: 用户决定数量增长后同样需要上限。
        return self.renderer.render(
            _TEMPLATE,
            documents=documents,
            open_findings=shown_findings,
            hidden_open_count=len(open_findings) - len(shown_findings),
            closed_counts=dict(closed_counts),
            decisions=state["decisions"],
            unrecorded_reads=list(unrecorded_reads),
            forced_reads=list(forced_reads),
        )

    def reset(self) -> None:
        """清空审阅状态。

        Returns:
            None。
        """
        self.store.clear()

    def _build_document_view(
        self,
        snapshot: DocumentSnapshot,
        *,
        read_revision: str,
        reviewed: set[str],
    ) -> dict[str, Any]:
        """把文档结构和已审阅块合成为模板使用的大纲进度视图。

        大纲只保留级别不超过 outline_max_level 的标题，章节起始标题始终保留。

        Args:
            snapshot: 文档当前结构索引。
            read_revision: 最近一次读取时的 revision。
            reviewed: 已审阅内容块 ID 集合。

        Returns:
            含 path、stale、reviewed_count、block_count、outline 的字典；
            outline 每项含 id、level、title、section。section 仅章节起始标题有，
            含 status（done / partial / pending）、last_id（章节末块 ID）、
            block_count（章节块数），供模型直接用作 start_id / end_id；其余标题为 None。
        """
        sections: dict[str, dict[str, Any]] = {}
        for section in snapshot.sections:
            done = sum(block_id in reviewed for block_id in section.block_ids)
            sections[section.id] = {
                "status": (
                    "done"
                    if done == len(section.block_ids)
                    else "partial"
                    if done
                    else "pending"
                ),
                "last_id": section.block_ids[-1],
                "block_count": len(section.block_ids),
            }

        heading_ids = {heading.id for heading in snapshot.headings}
        outline: list[dict[str, Any]] = [
            {
                "id": section.id,
                "level": 1,
                "title": "（首个标题前内容）",
                "section": sections[section.id],
            }
            for section in snapshot.sections
            if section.id not in heading_ids
        ]
        outline.extend(
            {
                "id": heading.id,
                "level": heading.level,
                "title": heading.title,
                "section": sections.get(heading.id),
            }
            for heading in snapshot.headings
            if heading.level <= self.outline_max_level or heading.id in sections
        )
        return {
            "path": snapshot.path,
            "stale": snapshot.revision != read_revision,
            "reviewed_count": sum(
                block_id in reviewed for block_id in snapshot.block_ids
            ),
            "block_count": len(snapshot.block_ids),
            "outline": outline,
        }

    def _truncate(self, text: str) -> str:
        """把问题描述限制在 finding_max_chars 内。

        Args:
            text: 原始问题描述。

        Returns:
            未超限的原文，或以省略号结尾的截断文本。
        """
        if len(text) <= self.finding_max_chars:
            return text
        return f"{text[: self.finding_max_chars - 1]}…"
