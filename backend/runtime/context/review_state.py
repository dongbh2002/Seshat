"""审阅状态上下文，把 ReviewStateStore 与文档结构渲染为有体积上限的提示词。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from backend.config import ReviewStateSettings
from backend.session import ReviewStateStore
from backend.templating import PromptRenderer
from backend.utils.docx import DocumentIndex, DocumentSnapshot

_TEMPLATE = "review_state_prompt.j2"


class VersionNoticeSource(Protocol):
    """提供文档版本待确认提示的组件（信号采集）；未接入时审阅状态不展示该部分。"""

    def get_version_notice(self, revision: str) -> Mapping[str, Any] | None:
        """返回文档版本的待确认提示。

        Args:
            revision: 文档当前 revision。

        Returns:
            待确认提示；无需确认时为 None。
        """
        ...


class ReviewStateContext:
    """按配置裁剪大纲与问题列表，生成注入模型上下文的审阅工作状态。"""

    def __init__(
        self,
        settings: ReviewStateSettings,
        store: ReviewStateStore,
        document_index: DocumentIndex,
        renderer: PromptRenderer,
        version_notices: VersionNoticeSource | None,
    ) -> None:
        """初始化审阅状态上下文。

        Args:
            settings: 审阅状态展示上限配置。
            store: 跨轮次保留的结构化审阅状态。
            document_index: 为已登记文档提供大纲和章节的结构索引。
            renderer: 提示词模板渲染器。
            version_notices: 文档版本待确认提示的来源；不采集信号时为 None。

        Returns:
            None。
        """
        self.outline_max_level = settings.outline_max_level  # 大纲展示的最大标题级别。
        self.max_open_findings = settings.max_open_findings  # 展示的 open 问题上限。
        self.finding_max_chars = settings.finding_max_chars  # 单条问题展示字符上限。
        self.store = store  # 审阅状态存储。
        self.document_index = document_index  # 文档结构索引。
        self.renderer = renderer  # 提示词模板渲染器。
        self.version_notices = version_notices  # 文档版本待确认提示的来源。

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
        revisions: dict[str, str] = {}  # 本次已加载文档的路径到当前 revision。
        for path, document in state["documents"].items():
            try:
                snapshot = self.document_index.load(path)
            except (OSError, ValueError) as error:
                documents.append({"path": path, "error": str(error)})
                continue
            revisions[path] = snapshot.revision
            documents.append(
                self._build_document_view(
                    snapshot,
                    seen_revision=document["seen_revision"],
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
            version_notices=self._collect_version_notices(state, revisions),
            unrecorded_reads=list(unrecorded_reads),
            forced_reads=list(forced_reads),
        )

    def _collect_version_notices(
        self,
        state: Mapping[str, Any],
        revisions: Mapping[str, str],
    ) -> list[Mapping[str, Any]]:
        """收集本会话涉及的文档（已读取或有问题记录）的版本待确认提示。

        Args:
            state: ReviewStateStore.snapshot 的结果。
            revisions: 已加载文档的路径到当前 revision，其余路径按需加载。

        Returns:
            去重后的待确认提示；未接入提示来源时为空。
        """
        if self.version_notices is None:
            return []
        paths = dict.fromkeys(
            [*state["documents"], *(finding["path"] for finding in state["findings"])]
        )
        notices: dict[str, Mapping[str, Any]] = {}
        for path in paths:
            revision = revisions.get(path)
            if revision is None:
                try:
                    revision = self.document_index.load(path).revision
                except (OSError, ValueError):
                    continue
            notice = self.version_notices.get_version_notice(revision)
            if notice is not None:
                notices.setdefault(notice["revision"], notice)
        return list(notices.values())

    def sync_documents(self) -> None:
        """按磁盘当前内容同步已登记文档：登记会话外修改，撤销内容已变化块的已审阅标记。

        当前无法读取的文档跳过，渲染时会提示错误。

        Returns:
            None。
        """
        for path in self.store.tracked_paths():
            try:
                snapshot = self.document_index.load(path)
            except (OSError, ValueError):
                continue
            self.store.sync_document(
                path,
                snapshot.revision,
                snapshot.get_block_texts(snapshot.block_ids),
            )

    def reset(self) -> None:
        """清空审阅状态。

        Returns:
            None。
        """
        self.store.clear()

    def export_state(self) -> dict[str, Any]:
        """导出审阅状态，供会话持久化。

        Returns:
            ReviewStateStore.export_state 的结果。
        """
        return self.store.export_state()

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """恢复审阅状态，并立即与磁盘上的文档同步。

        会话中断期间文档可能在会话外被修改（如 Word）；同步后历史中的旧读取
        会被判为过时并由压缩清理，内容已变化的块不再算已审阅。

        Args:
            state: ``export_state`` 导出的字典。

        Returns:
            None。
        """
        self.store.restore_state(state)
        self.sync_documents()

    def _build_document_view(
        self,
        snapshot: DocumentSnapshot,
        *,
        seen_revision: str,
        reviewed: set[str],
    ) -> dict[str, Any]:
        """把文档结构和已审阅块合成为模板使用的大纲进度视图。

        大纲只保留级别不超过 outline_max_level 的标题，章节起始标题始终保留。

        Args:
            snapshot: 文档当前结构索引。
            seen_revision: 模型最后读取或自行修改后得到的 revision。
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
            "stale": snapshot.revision != seen_revision,
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
