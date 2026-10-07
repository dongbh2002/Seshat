"""记忆读取：按当前会话涉及的文档选取四级生效记忆，供主对话上下文与审阅子任务使用。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from backend.config import MemorySettings
from backend.memory.models import MemoryItem, MemoryScope
from backend.memory.scoring import MemoryScorer
from backend.memory.store import MemoryStore
from backend.session import ReviewStateStore
from backend.signals import DocumentLineage
from backend.utils.docx import DocumentIndex


class MemoryRetriever:
    """选取文档级（只取相关文档）、用户级、课题组级（导师要求与共性）与通用级的生效记忆，各级按得分截断并为新条目留名额。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        scorer: MemoryScorer,
        settings: MemorySettings,
        tenant_id: str,
        user_id: str,
        review_state: ReviewStateStore,
        document_index: DocumentIndex,
        lineage: DocumentLineage,
    ) -> None:
        """初始化记忆读取。

        Args:
            store: 记忆存储。
            scorer: 记忆评分，按得分与新近程度选取条目。
            settings: 记忆配置，提供各级条目上限。
            tenant_id: 当前课题组 ID。
            user_id: 当前用户 ID。
            review_state: 当前会话的审阅状态，用于确定会话涉及的文档。
            document_index: 文档结构索引，用于取文档当前 revision。
            lineage: 版本链，用于由 revision 找到所属文档。

        Returns:
            None。
        """
        self.store = store  # 记忆存储。
        self.scorer = scorer  # 记忆评分。
        self.settings = settings  # 记忆配置。
        self.tenant_id = tenant_id  # 当前课题组 ID。
        self.user_id = user_id  # 当前用户 ID。
        self.review_state = review_state  # 当前会话的审阅状态。
        self.document_index = document_index  # 文档结构索引。
        self.lineage = lineage  # 版本链。

    def select_memory(self) -> dict[str, Any]:
        """选取当前会话注入上下文的记忆。

        Returns:
            ``profile``（用户画像事实）、``documents``（本会话涉及文档的文档级记忆：
            title、entries）、``advisor``（导师要求）、``tenant``（课题组共性）、
            ``user``、``general``，均为条目内容并已按上限截断。
        """
        documents = []
        for paper_id, title in self.session_papers():
            entries = _contents(self._document_items(paper_id))
            if entries:
                documents.append({"title": title, "entries": entries})
        return {"documents": documents, **self._shared()}

    def rules_for_review(self, revision: str) -> list[dict[str, str]]:
        """返回审阅某个文档版本时应遵循的记忆，按“文档 > 导师要求 > 课题组共性 > 用户 > 通用”排列。

        Args:
            revision: 被审阅文档的 revision；只取该文档的文档级记忆。

        Returns:
            ``{"level", "content"}`` 列表；用户画像事实不含在内。
        """
        paper_id = self.lineage.paper_of(revision)
        rules = [
            {"level": "document", "content": item.content}
            for item in (self._document_items(paper_id) if paper_id else [])
        ]
        shared = self._shared()
        for level, key in (
            ("advisor", "advisor"),
            ("tenant", "tenant"),
            ("user", "user"),
            ("global", "general"),
        ):
            rules.extend({"level": level, "content": entry} for entry in shared[key])
        return rules

    def session_papers(self) -> list[tuple[str, str]]:
        """找出本会话涉及（已读取或有问题记录）且来源已确认的文档。

        Returns:
            按出现顺序去重的 (文档 ID, 标题)。
        """
        state = self.review_state.snapshot()
        paths = dict.fromkeys(
            [*state["documents"], *(finding["path"] for finding in state["findings"])]
        )
        papers: dict[str, str] = {}
        for path in paths:
            try:
                revision = self.document_index.load(path).revision
            except (OSError, ValueError):
                continue
            paper_id = self.lineage.paper_of(revision)
            record = self.lineage.get(revision)
            if paper_id and record is not None:
                papers.setdefault(paper_id, record.title)
        return list(papers.items())

    def _shared(self) -> dict[str, list[str]]:
        """选取与具体文档无关的用户级、课题组级与通用级记忆。

        Returns:
            ``profile``、``advisor``、``tenant``、``user``、``general`` 的条目内容。
        """
        settings = self.settings
        user_items = self._active(
            MemoryScope("user", self.tenant_id, self.user_id), settings.user_max_items
        )
        tenant_items = self._active(
            MemoryScope("tenant", self.tenant_id), settings.tenant_max_items
        )
        return {
            "profile": _contents(item for item in user_items if item.kind == "fact"),
            "advisor": _contents(
                item for item in tenant_items if item.source == "agent"
            ),
            "tenant": _contents(
                item for item in tenant_items if item.source == "promoted"
            ),
            "user": _contents(item for item in user_items if item.kind == "rule"),
            "general": _contents(
                self._active(MemoryScope("global"), settings.global_max_items)
            ),
        }

    def _document_items(self, paper_id: str) -> list[MemoryItem]:
        """读取一篇文档的生效记忆。

        Args:
            paper_id: 文档 ID。

        Returns:
            按支持数截断的生效条目。
        """
        return self._active(
            MemoryScope("document", self.tenant_id, self.user_id, paper_id),
            self.settings.document_max_items,
        )

    def _active(self, scope: MemoryScope, limit: int) -> list[MemoryItem]:
        """读取归属下注入上下文的生效条目：按得分取前列，并为最近新增的条目留名额。

        Args:
            scope: 记忆归属。
            limit: 条目上限。

        Returns:
            生效条目列表。
        """
        return self.scorer.select_for_context(
            self.store.load(scope), limit, datetime.now().astimezone()
        )


def _contents(items: Iterable[MemoryItem]) -> list[str]:
    """取条目内容。

    Args:
        items: 条目。

    Returns:
        内容列表。
    """
    return [item.content for item in items]
