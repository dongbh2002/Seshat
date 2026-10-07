"""用户级论文版本链：保存见过的每个 DOCX 版本快照，记录版本所属论文与父子关系，并按内容匹配疑似历史版本。

目录布局（snapshots/<tenant_id>/<user_id>/）：
    <revision>.docx   版本快照，revision 为文件内容 SHA-256
    lineage.json      版本记录 {"schema_version": 1, "versions": {revision: VersionRecord}}
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.signals.document import DocxVersion, fingerprint_similarity
from backend.utils.docx import calculate_revision
from backend.utils.time_id import new_time_id

LINEAGE_FILENAME = "lineage.json"  # 快照目录下的版本记录文件名。
REVISION_PREFIX_LENGTH = 12  # 展示给模型和用户的 revision 前缀长度。
VERSION_STATUSES = ("pending", "confirmed")  # 版本状态：待用户确认来源、已确认。
VERSION_ORIGINS = ("observed", "written")  # 版本来源：工具读到、Seshat 写出。
_SCHEMA_VERSION = 1  # 版本记录文件格式版本。
_MIN_PREFIX_LENGTH = 8  # 按前缀查找 revision 时的最短长度。


@dataclass
class VersionRecord:
    """一个见过的文档版本。"""

    revision: str  # 文档内容 SHA-256，也是快照文件名。
    paper_id: str  # 所属论文 ID；来源未确认时为空。
    parent_revision: str  # 父版本 revision；没有时为空。
    status: str  # 版本状态，取值见 VERSION_STATUSES。
    origin: str  # 版本来源，取值见 VERSION_ORIGINS。
    path: str  # 首次见到时在工作区的相对路径。
    title: str  # 首个标题或首段文字，便于用户辨认。
    content_key: str  # 各修订视图正文与批注的哈希，内容完全相同的版本相同。
    session_id: str  # 首次见到时的会话 ID；未知为空。
    created_at: str  # 首次见到的时间，ISO 8601。
    candidates: list[dict[str, Any]] = field(default_factory=list)  # 疑似历史版本。


class DocumentLineage:
    """读写用户的版本快照与版本记录，并按内容指纹查找疑似历史版本。"""

    def __init__(
        self,
        directory: Path,
        *,
        match_threshold: float,
        max_candidates: int,
    ) -> None:
        """加载版本记录，目录在首次写入时创建。

        Args:
            directory: 当前用户的快照目录。
            match_threshold: 列为疑似历史版本所需的最低内容相似度。
            max_candidates: 最多列出的疑似历史版本数。

        Returns:
            None。

        Raises:
            ValueError: 版本记录文件格式版本不符。
        """
        self.directory = directory  # 快照与版本记录所在目录。
        self.path = directory / LINEAGE_FILENAME  # 版本记录文件路径。
        self.match_threshold = match_threshold  # 疑似历史版本的最低相似度。
        self.max_candidates = max_candidates  # 最多列出的疑似历史版本数。
        self._records = self._load()  # revision 到版本记录。
        self._versions: dict[str, DocxVersion] = {}  # revision 到已解析的快照。

    def get(self, revision: str) -> VersionRecord | None:
        """按完整 revision 查找版本记录。

        Args:
            revision: 文档内容 SHA-256。

        Returns:
            版本记录；未见过时为 None。
        """
        return self._records.get(revision)

    def paper_of(self, revision: str) -> str:
        """返回版本所属的论文 ID。

        Args:
            revision: 文档内容 SHA-256。

        Returns:
            论文 ID；版本未登记或来源未确认时为空字符串。
        """
        record = self._records.get(revision)
        return record.paper_id if record is not None else ""

    def paper_title(self, paper_id: str) -> str:
        """返回论文最近见到的版本标题。

        Args:
            paper_id: 论文 ID。

        Returns:
            标题；没有该论文的版本时为空字符串。
        """
        records = [
            record for record in self._records.values() if record.paper_id == paper_id
        ]
        latest = max(records, key=lambda record: record.created_at, default=None)
        return latest.title if latest is not None else ""

    def resolve(self, revision_prefix: str) -> VersionRecord:
        """按 revision 或其前缀查找版本记录。

        Args:
            revision_prefix: 完整 revision，或不短于 8 位的前缀。

        Returns:
            唯一匹配的版本记录。

        Raises:
            ValueError: 前缀过短、没有匹配或匹配不唯一。
        """
        prefix = revision_prefix.strip().lower()
        if len(prefix) < _MIN_PREFIX_LENGTH:
            raise ValueError(
                f"revision 前缀至少 {_MIN_PREFIX_LENGTH} 位: {revision_prefix}"
            )
        matches = [
            record for key, record in self._records.items() if key.startswith(prefix)
        ]
        if len(matches) != 1:
            state = "不存在" if not matches else "不唯一"
            raise ValueError(f"历史版本 {revision_prefix} {state}")
        return matches[0]

    def snapshot_path(self, revision: str) -> Path:
        """返回版本快照路径。

        Args:
            revision: 文档内容 SHA-256。

        Returns:
            <快照目录>/<revision>.docx（不保证存在）。
        """
        return self.directory / f"{revision}.docx"

    def save_snapshot(self, revision: str, source: Path) -> bool:
        """把工作区文档复制为版本快照，已存在时跳过。

        Args:
            revision: 工具返回的文档 revision。
            source: 工作区中的文档路径。

        Returns:
            快照可用时为 True；文件在工具返回后又被修改、内容与 revision 不符时为 False。

        Raises:
            OSError: 文件复制失败。
        """
        target = self.snapshot_path(revision)
        if target.is_file():
            return True
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        if calculate_revision(temporary) != revision:
            temporary.unlink()
            return False
        os.replace(temporary, target)
        return True

    def load_version(self, revision: str) -> DocxVersion:
        """返回版本快照的解析结果，同一进程内缓存。

        Args:
            revision: 文档内容 SHA-256。

        Returns:
            快照对应的 DocxVersion。

        Raises:
            FileNotFoundError: 快照不存在。
        """
        if revision not in self._versions:
            path = self.snapshot_path(revision)
            if not path.is_file():
                raise FileNotFoundError(f"版本快照不存在: {path}")
            self._versions[revision] = DocxVersion(path)
        return self._versions[revision]

    def add(self, record: VersionRecord) -> None:
        """登记新版本；已确认但没有论文 ID 也没有父版本的版本视为一篇新论文。

        Args:
            record: 版本记录，其快照须已保存。

        Returns:
            None。

        Raises:
            ValueError: 状态或来源取值无效。
            OSError: 版本记录写入失败。
        """
        if record.status not in VERSION_STATUSES:
            raise ValueError(f"版本状态无效: {record.status}")
        if record.origin not in VERSION_ORIGINS:
            raise ValueError(f"版本来源无效: {record.origin}")
        if (
            record.status == "confirmed"
            and not record.paper_id
            and not record.parent_revision
        ):
            record.paper_id = new_time_id(datetime.now())
        self._records[record.revision] = record
        self._save()

    def find_identical(self, content_key: str) -> VersionRecord | None:
        """查找内容完全相同的已确认版本。

        Args:
            content_key: 待查版本的内容键。

        Returns:
            最近见到的同内容已确认版本；没有时为 None。
        """
        matches = [
            record
            for record in self._records.values()
            if record.status == "confirmed" and record.content_key == content_key
        ]
        return max(matches, key=lambda record: record.created_at, default=None)

    def find_candidates(
        self,
        version: DocxVersion,
        *,
        exclude_revision: str,
    ) -> list[tuple[VersionRecord, float]]:
        """按内容相似度查找疑似历史版本，每篇论文只取最相似的一个版本。

        取最相似而非最新的版本作为候选：用户交给导师的可能是原稿而非 Seshat 修改后的版本。

        Args:
            version: 待匹配的新版本。
            exclude_revision: 不参与匹配的 revision（新版本自身）。

        Returns:
            按相似度从高到低排列的 (版本记录, 相似度)，最多 max_candidates 个。
        """
        # TODO: 版本很多时每次匹配都要解析全部快照，可把指纹持久化到版本记录中。
        best: dict[str, tuple[VersionRecord, float]] = {}
        for record in self._records.values():
            if (
                record.status != "confirmed"
                or not record.paper_id
                or record.revision == exclude_revision
            ):
                continue
            try:
                fingerprint = self.load_version(record.revision).fingerprint
            except (OSError, ValueError):
                continue
            score = fingerprint_similarity(version.fingerprint, fingerprint)
            current = best.get(record.paper_id)
            if score >= self.match_threshold and (
                current is None or score > current[1]
            ):
                best[record.paper_id] = (record, score)
        ranked = sorted(best.values(), key=lambda item: item[1], reverse=True)
        return ranked[: self.max_candidates]

    def pending_root(self, revision: str) -> VersionRecord | None:
        """沿父版本向上查找待确认来源的版本。

        Seshat 在待确认版本上写出的新版本，确认时应确认其待确认的祖先。

        Args:
            revision: 起始 revision。

        Returns:
            自身或最近的待确认祖先；都已确认或未登记时为 None。
        """
        record = self._records.get(revision)
        visited: set[str] = set()
        while record is not None and record.revision not in visited:
            if record.status == "pending":
                return record
            visited.add(record.revision)
            record = self._records.get(record.parent_revision)
        return None

    def confirm(self, revision: str, *, parent_revision: str) -> VersionRecord:
        """确认待确认版本的来源，并让确认前在其上写出的后代归入同一论文。

        Args:
            revision: 待确认版本的 revision。
            parent_revision: 用户确认的父版本 revision；不是任何历史版本的新版时为空。

        Returns:
            确认后的版本记录。

        Raises:
            KeyError: 版本或父版本未登记。
            OSError: 版本记录写入失败。
        """
        record = self._records[revision]
        record.parent_revision = parent_revision
        record.paper_id = (
            self._records[parent_revision].paper_id
            if parent_revision
            else new_time_id(datetime.now())
        )
        record.status = "confirmed"
        record.candidates = []
        changed = True
        while changed:
            changed = False
            for child in self._records.values():
                parent = self._records.get(child.parent_revision)
                if not child.paper_id and parent is not None and parent.paper_id:
                    child.paper_id = parent.paper_id
                    changed = True
        self._save()
        return record

    def _load(self) -> dict[str, VersionRecord]:
        """读取版本记录文件。

        Returns:
            revision 到版本记录；文件不存在时为空。

        Raises:
            ValueError: 文件格式版本不符。
        """
        if not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(f"版本记录格式版本不符: {self.path}")
        return {
            revision: VersionRecord(**record)
            for revision, record in data["versions"].items()
        }

    def _save(self) -> None:
        """原子写入版本记录文件。

        Returns:
            None。

        Raises:
            OSError: 文件写入失败。
        """
        # TODO: 同一用户在两个进程中同时运行会互相覆盖，需要文件锁。
        self.directory.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": _SCHEMA_VERSION,
            "versions": {
                revision: asdict(record) for revision, record in self._records.items()
            },
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, self.path)
