"""审阅状态存储，沉淀长文档审阅过程中的文档、进度、问题和用户决定。

工具与 Hook 负责写入，上下文引擎和压缩策略负责读取，彼此只依赖本存储；
export_state / restore_state 供会话持久化无损导出与恢复。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from backend.utils.docx import is_position_based_block_id

FINDING_STATUSES = ("open", "accepted", "rejected", "resolved")  # 问题允许的状态。
_FINGERPRINT_LENGTH = 16  # 块正文指纹保留的十六进制位数。


def _fingerprint(text: str) -> str:
    """计算块正文指纹，用于判断已审阅块的内容之后是否变化。

    Args:
        text: 块正文（接受修订视图）。

    Returns:
        正文 SHA-256 的前 ``_FINGERPRINT_LENGTH`` 位十六进制。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


@dataclass
class ReviewFinding:
    """一条定位到文档内容块的审阅问题。"""

    id: str  # 会话内唯一的问题编号，例如 F1。
    path: str  # 问题所在文档的相对路径。
    block_id: str  # read_document 返回的内容块 ID。
    excerpt: str  # 记录问题时该内容块的原文（接受修订视图），文档之后被改也不丢，供记忆沉淀。
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
        self.seen_revision = revision  # 模型最后读取或自行修改后得到的 revision。
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

    def to_dict(self) -> dict[str, Any]:
        """导出全部版本记录，供会话持久化。

        Returns:
            与实例属性同名的键组成的字典。
        """
        return {
            "latest_revision": self.latest_revision,
            "seen_revision": self.seen_revision,
            "latest_sequence": self.latest_sequence,
            "sequence_by_revision": dict(self.sequence_by_revision),
            "block_change_sequence": dict(self.block_change_sequence),
            "structure_change_sequence": self.structure_change_sequence,
            "unknown_change_sequence": self.unknown_change_sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DocumentVersions:
        """从 ``to_dict`` 的导出结果重建版本记录。

        Args:
            data: ``to_dict`` 导出的字典。

        Returns:
            与导出时状态一致的版本记录。
        """
        versions = cls(data["latest_revision"])
        versions.seen_revision = data["seen_revision"]
        versions.latest_sequence = data["latest_sequence"]
        versions.sequence_by_revision = dict(data["sequence_by_revision"])
        versions.block_change_sequence = dict(data["block_change_sequence"])
        versions.structure_change_sequence = data["structure_change_sequence"]
        versions.unknown_change_sequence = data["unknown_change_sequence"]
        return versions


class ReviewStateStore:
    """保存当前会话的结构化审阅状态，内容不随上下文压缩丢失。"""

    def __init__(self) -> None:
        """初始化空审阅状态。

        Returns:
            None。
        """
        self._versions: dict[str, DocumentVersions] = {}  # 文档路径到版本记录。
        self._reviewed: dict[str, dict[str, str]] = {}  # 路径到 {已审阅块: 指纹}。
        self._findings: dict[str, ReviewFinding] = {}  # 问题编号到审阅问题的映射。
        self._decisions: list[str] = []  # 用户明确表达的审阅决定或偏好。
        self._next_number = 1  # 下一个问题编号的序号。
        self._lock = threading.Lock()  # 工具在线程池执行，读写需互斥。

    def track_document(self, path: str, revision: str) -> None:
        """登记模型读取到的文档 revision。

        Args:
            path: 文档相对路径。
            revision: 文档内容 SHA-256。

        Returns:
            None。
        """
        with self._lock:
            self._register_revision(path, revision).seen_revision = revision
            self._reviewed.setdefault(path, {})

    def sync_document(
        self,
        path: str,
        revision: str,
        block_texts: Mapping[str, str],
    ) -> None:
        """按文档当前内容同步：登记会话外修改，撤销正文已变或已不存在的块的已审阅标记。

        不更新 seen_revision：模型尚未读到新内容，审阅状态据此提示文档已被修改。
        未登记的文档忽略。

        Args:
            path: 文档相对路径。
            revision: 文档当前 revision。
            block_texts: 文档当前全部块 ID 到正文（接受修订视图）的映射。

        Returns:
            None。
        """
        with self._lock:
            if path not in self._versions:
                return
            self._register_revision(path, revision)
            reviewed = self._reviewed.setdefault(path, {})
            for block_id, fingerprint in list(reviewed.items()):
                text = block_texts.get(block_id)
                if text is None or _fingerprint(text) != fingerprint:
                    del reviewed[block_id]
            # TODO: 会话外修改后 open 问题所在块可能已改动或错位（位置型 ID），
            # 可比对 excerpt 提示模型复核。

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
            if versions.seen_revision == previous_revision:
                versions.seen_revision = revision
            for block_id in changed_block_ids:
                versions.block_change_sequence[block_id] = versions.latest_sequence
            if structural:
                versions.structure_change_sequence = versions.latest_sequence
            self._reviewed.setdefault(path, {})

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

    def mark_reviewed(self, path: str, block_texts: Mapping[str, str]) -> None:
        """记录文档中已审阅的内容块及其当前正文指纹。

        Args:
            path: 文档相对路径。
            block_texts: 已审阅块 ID 到其当前正文（接受修订视图）的映射。

        Returns:
            None。
        """
        fingerprints = {
            block_id: _fingerprint(text) for block_id, text in block_texts.items()
        }
        with self._lock:
            self._reviewed.setdefault(path, {}).update(fingerprints)

    def unmark_reviewed(self, path: str, block_ids: Iterable[str]) -> None:
        """取消内容块的已审阅标记，用于内容被修改后需要重新审阅的块。

        Args:
            path: 文档相对路径。
            block_ids: 需要取消标记的内容块 ID；未标记的 ID 会被忽略。

        Returns:
            None。
        """
        with self._lock:
            reviewed = self._reviewed.get(path, {})
            for block_id in block_ids:
                reviewed.pop(block_id, None)

    def get_unreviewed(self, path: str, block_ids: Sequence[str]) -> list[str]:
        """筛出尚未标记为已审阅的内容块。

        Args:
            path: 文档相对路径。
            block_ids: 待检查的内容块 ID，结果保持该顺序。

        Returns:
            未审阅的内容块 ID 列表。
        """
        with self._lock:
            reviewed = self._reviewed.get(path, {})
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

    def add_finding(
        self,
        path: str,
        block_id: str,
        issue: str,
        *,
        excerpt: str,
    ) -> str:
        """新增一条状态为 open 的审阅问题。

        Args:
            path: 文档相对路径。
            block_id: 问题所在内容块 ID。
            issue: 问题描述及修改建议。
            excerpt: 该内容块当前的原文。

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
                excerpt=excerpt,
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

    def get_finding(self, finding_id: str) -> ReviewFinding:
        """按编号查询问题。

        Args:
            finding_id: 问题编号。

        Returns:
            问题的独立副本。

        Raises:
            ValueError: 问题编号不存在。
        """
        with self._lock:
            if finding_id not in self._findings:
                raise ValueError(f"问题编号不存在: {finding_id}")
            return replace(self._findings[finding_id])

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
            ``documents``：路径到 ``seen_revision`` 与 ``reviewed_block_ids`` 的映射；
            ``findings``：问题字典列表；``decisions``：用户决定列表。
        """
        with self._lock:
            return {
                "documents": {
                    path: {
                        "seen_revision": versions.seen_revision,
                        "reviewed_block_ids": sorted(self._reviewed.get(path, {})),
                    }
                    for path, versions in self._versions.items()
                },
                "findings": [asdict(finding) for finding in self._findings.values()],
                "decisions": list(self._decisions),
            }

    def tracked_paths(self) -> list[str]:
        """返回已登记版本的文档路径。

        Returns:
            文档相对路径列表。
        """
        with self._lock:
            return list(self._versions)

    def export_state(self) -> dict[str, Any]:
        """无损导出全部状态，供会话持久化；与 ``snapshot`` 不同，含版本序列与编号。

        Returns:
            ``versions``：路径到 ``DocumentVersions.to_dict`` 的映射；
            ``reviewed``：路径到 {已审阅块 ID: 正文指纹}；``findings``：问题字典列表；
            ``decisions``：用户决定列表；``next_finding_number``：下一个问题序号。
        """
        with self._lock:
            return {
                "versions": {
                    path: versions.to_dict()
                    for path, versions in self._versions.items()
                },
                "reviewed": {
                    path: dict(sorted(marks.items()))
                    for path, marks in self._reviewed.items()
                },
                "findings": [asdict(finding) for finding in self._findings.values()],
                "decisions": list(self._decisions),
                "next_finding_number": self._next_number,
            }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """用 ``export_state`` 的导出结果整体替换当前状态。

        Args:
            state: ``export_state`` 导出的字典。

        Returns:
            None。

        Raises:
            KeyError: 缺少必需的键。
            TypeError: 问题字段与 ReviewFinding 不匹配。
        """
        versions = {
            path: DocumentVersions.from_dict(data)
            for path, data in state["versions"].items()
        }
        reviewed = {path: dict(marks) for path, marks in state["reviewed"].items()}
        findings = [ReviewFinding(**finding) for finding in state["findings"]]
        with self._lock:
            self._versions = versions
            self._reviewed = reviewed
            self._findings = {finding.id: finding for finding in findings}
            self._decisions = list(state["decisions"])
            self._next_number = state["next_finding_number"]

    def _register_revision(self, path: str, revision: str) -> DocumentVersions:
        """登记文档 revision，调用方须已持有锁。

        首次见到的 revision 且文档已登记过，说明文档在会话之外被修改（如 Word），
        无法得知改了哪些块，此前的全部读取都视为过时。

        Args:
            path: 文档相对路径。
            revision: 文档内容 SHA-256。

        Returns:
            该文档的版本记录。
        """
        versions = self._versions.get(path)
        if versions is None:
            versions = self._versions[path] = DocumentVersions(revision)
        elif revision not in versions.sequence_by_revision:
            versions.advance(revision)
            versions.unknown_change_sequence = versions.latest_sequence
        else:
            versions.latest_revision = revision
        return versions

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
