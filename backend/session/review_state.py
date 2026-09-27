"""审阅状态存储，沉淀长文档审阅过程中的文档、进度、问题和用户决定。

工具与 Hook 负责写入，上下文引擎和压缩策略负责读取，彼此只依赖本存储。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from backend.utils.docx import is_position_based_block_id

FINDING_STATUSES = ("open", "accepted", "rejected", "resolved")  # 问题允许的状态。


@dataclass
class ReviewFinding:
    """一条定位到文档内容块的审阅问题。"""

    id: str  # 会话内唯一的问题编号，例如 F1。
    path: str  # 问题所在文档的相对路径。
    block_id: str  # read_document 返回的内容块 ID。
    issue: str  # 问题描述及修改建议。
    status: str  # 问题状态，取值见 FINDING_STATUSES。


class DocumentVersions:
    """单个文档在会话中的版本序号与块修改记录，用于判断旧读取结果是否过时。"""

    def __init__(self, revision: str) -> None:
        """以首次登记的 revision 作为序号 1 初始化。

        Args:
            revision: 首次登记的文档 revision。

        Returns:
            None。
        """
        self.latest_revision = revision  # 最新已知 revision。
        self.latest_sequence = 1  # 最新 revision 的序号。
        self.sequence_by_revision: dict[str, int] = {revision: 1}  # revision 到序号。
        self.block_change_sequence: dict[str, int] = {}  # 块 ID 到最后被修改时的序号。
        self.structure_change_sequence = 0  # 最后一次插入或删除块时的序号。
        self.unknown_change_sequence = 0  # 最后一次会话外修改被发现时的序号。

    def advance(self, revision: str) -> None:
        """登记一个新的最新 revision 并分配下一个序号。

        Args:
            revision: 新的文档 revision。

        Returns:
            None。
        """
        self.latest_sequence += 1
        self.sequence_by_revision[revision] = self.latest_sequence
        self.latest_revision = revision


class ReviewStateStore:
    """保存当前会话的结构化审阅状态，内容不随上下文压缩丢失。"""

    def __init__(self) -> None:
        """初始化空审阅状态。

        Returns:
            None。
        """
        self._versions: dict[str, DocumentVersions] = {}  # 文档路径到版本记录。
        self._reviewed: dict[str, set[str]] = {}  # 文档路径到已审阅块 ID 集合。
        self._findings: dict[str, ReviewFinding] = {}  # 问题编号到审阅问题的映射。
        self._decisions: list[str] = []  # 用户明确表达的审阅决定或偏好。
        self._next_number = 1  # 下一个问题编号的序号。
        self._lock = threading.Lock()  # 工具在线程池执行，读写需互斥。
        # TODO: 按会话持久化到租户目录，并作为导师风格记忆的沉淀来源。

    def track_document(self, path: str, revision: str) -> None:
        """登记读取到的文档 revision。

        首次见到的 revision 且文档已登记过，说明文档在会话之外被修改（如 Word），
        无法得知改了哪些块，此前的全部读取都视为过时。

        Args:
            path: 文档相对路径。
            revision: 文档内容 SHA-256。

        Returns:
            None。
        """
        with self._lock:
            versions = self._versions.get(path)
            if versions is None:
                self._versions[path] = DocumentVersions(revision)
            elif revision not in versions.sequence_by_revision:
                versions.advance(revision)
                versions.unknown_change_sequence = versions.latest_sequence
            else:
                versions.latest_revision = revision
            self._reviewed.setdefault(path, set())

    def record_changes(
        self,
        path: str,
        previous_revision: str,
        revision: str,
        changed_block_ids: Iterable[str],
        *,
        structural: bool,
    ) -> None:
        """登记一次会话内的原地修改：新版本及其中内容发生变化的块。

        Args:
            path: 文档相对路径。
            previous_revision: 修改前的 revision。
            revision: 修改后的 revision。
            changed_block_ids: 内容发生变化的块 ID。
            structural: 是否插入或删除了块，位置型块 ID 可能因此偏移。

        Returns:
            None。
        """
        with self._lock:
            versions = self._versions.setdefault(
                path,
                DocumentVersions(previous_revision),
            )
            if previous_revision not in versions.sequence_by_revision:
                versions.advance(previous_revision)
            versions.advance(revision)
            for block_id in changed_block_ids:
                versions.block_change_sequence[block_id] = versions.latest_sequence
            if structural:
                versions.structure_change_sequence = versions.latest_sequence
            self._reviewed.setdefault(path, set())

    def get_stale_blocks(
        self,
        path: str,
        revision: str,
        block_ids: Sequence[str],
    ) -> set[str]:
        """找出某次读取结果中已被之后的修改淘汰的块。

        按位置生成的块 ID（段落无 w14:paraId）在插入、删除等结构变化后会整体
        错位，因此这类读取遇到之后的结构变化时整次视为过时。

        Args:
            path: 文档相对路径。
            revision: 读取时的 revision。
            block_ids: 需要检查的块 ID。

        Returns:
            过时的块 ID 集合。revision 无记录、之后发现过会话外修改、或含位置型
            ID 且之后有结构变化时，整次读取都不可信，返回全部块；文档未登记或
            读取的就是最新版本时返回空集合。
        """
        with self._lock:
            versions = self._versions.get(path)
            if versions is None or revision == versions.latest_revision:
                return set()
            read_sequence = versions.sequence_by_revision.get(revision)
            if (
                read_sequence is None
                or versions.unknown_change_sequence > read_sequence
                or (
                    versions.structure_change_sequence > read_sequence
                    and any(is_position_based_block_id(b) for b in block_ids)
                )
            ):
                return set(block_ids)
            return {
                block_id
                for block_id in block_ids
                if versions.block_change_sequence.get(block_id, 0) > read_sequence
            }

    def mark_reviewed(self, path: str, block_ids: Iterable[str]) -> None:
        """记录文档中已审阅的内容块。

        Args:
            path: 文档相对路径。
            block_ids: 已审阅的内容块 ID。

        Returns:
            None。
        """
        with self._lock:
            self._reviewed.setdefault(path, set()).update(block_ids)

    def unmark_reviewed(self, path: str, block_ids: Iterable[str]) -> None:
        """取消内容块的已审阅标记，用于内容被修改后需要重新审阅的块。

        Args:
            path: 文档相对路径。
            block_ids: 需要取消标记的内容块 ID；未标记的 ID 会被忽略。

        Returns:
            None。
        """
        with self._lock:
            self._reviewed.get(path, set()).difference_update(block_ids)

    def get_unreviewed(self, path: str, block_ids: Sequence[str]) -> list[str]:
        """筛出尚未标记为已审阅的内容块。

        Args:
            path: 文档相对路径。
            block_ids: 待检查的内容块 ID，结果保持该顺序。

        Returns:
            未审阅的内容块 ID 列表。
        """
        with self._lock:
            reviewed = self._reviewed.get(path, set())
            return [block_id for block_id in block_ids if block_id not in reviewed]

    def get_open_finding_blocks(self, path: str) -> set[str]:
        """返回文档中有 open 状态问题的块 ID，即接下来最可能被修改的块。

        Args:
            path: 文档相对路径。

        Returns:
            块 ID 集合。
        """
        with self._lock:
            return {
                finding.block_id
                for finding in self._findings.values()
                if finding.path == path and finding.status == "open"
            }

    def add_finding(self, path: str, block_id: str, issue: str) -> str:
        """新增一条状态为 open 的审阅问题。

        Args:
            path: 文档相对路径。
            block_id: 问题所在内容块 ID。
            issue: 问题描述及修改建议。

        Returns:
            新问题的编号。
        """
        with self._lock:
            finding_id = f"F{self._next_number}"
            self._next_number += 1
            self._findings[finding_id] = ReviewFinding(
                id=finding_id,
                path=path,
                block_id=block_id,
                issue=issue,
                status="open",
            )
            return finding_id

    def set_finding_status(self, finding_id: str, status: str) -> None:
        """更新已有问题的状态。

        Args:
            finding_id: 问题编号。
            status: 新状态，取值见 FINDING_STATUSES。

        Returns:
            None。

        Raises:
            ValueError: 问题编号不存在或状态值无效。
        """
        if status not in FINDING_STATUSES:
            raise ValueError(f"问题状态无效: {status}")
        with self._lock:
            if finding_id not in self._findings:
                raise ValueError(f"问题编号不存在: {finding_id}")
            self._findings[finding_id].status = status

    def add_decision(self, text: str) -> None:
        """记录一条用户审阅决定或偏好。

        Args:
            text: 决定内容。

        Returns:
            None。
        """
        with self._lock:
            if text not in self._decisions:
                self._decisions.append(text)

    def snapshot(self) -> dict[str, Any]:
        """导出当前状态的独立副本。

        Returns:
            ``documents``：路径到 ``revision`` 与 ``reviewed_block_ids`` 的映射；
            ``findings``：问题字典列表；``decisions``：用户决定列表。
        """
        with self._lock:
            return {
                "documents": {
                    path: {
                        "revision": versions.latest_revision,
                        "reviewed_block_ids": sorted(self._reviewed.get(path, set())),
                    }
                    for path, versions in self._versions.items()
                },
                "findings": [asdict(finding) for finding in self._findings.values()],
                "decisions": list(self._decisions),
            }

    def clear(self) -> None:
        """清空全部审阅状态。

        Returns:
            None。
        """
        with self._lock:
            self._versions.clear()
            self._reviewed.clear()
            self._findings.clear()
            self._decisions.clear()
            self._next_number = 1
