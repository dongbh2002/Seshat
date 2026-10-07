"""修改信号采集编排：登记见到的文档版本，判断是否需要用户确认来源，确认后比较版本并写入信号。

版本登记规则（observe）：
    已登记                                   → 跳过
    Seshat 在已登记版本上写出                  → 归入父版本所属论文，无需确认
    与某个已确认版本内容完全相同（如仅另存）     → 归入该版本所属论文，无需确认
    有疑似历史版本，或有未登记身份的作者          → 待确认（pending），由模型向用户确认
    其余                                      → 视为新论文；已登记作者的修订与批注直接采集
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.signals.comparison import SignalOrigin, VersionComparer
from backend.signals.lineage import (
    REVISION_PREFIX_LENGTH,
    DocumentLineage,
    VersionRecord,
)
from backend.signals.models import AUTHOR_ROLES, SIGNAL_ROLES
from backend.signals.roster import AuthorRoster
from backend.signals.store import SignalStore
from backend.utils.docx import calculate_revision, resolve_docx_path


class FeedbackCollector:
    """把文档版本的变化转为修改信号，供版本观察 Hook 与确认工具共用。"""

    def __init__(
        self,
        *,
        document_root: Path,
        lineage: DocumentLineage,
        roster: AuthorRoster,
        store: SignalStore,
        comparer: VersionComparer,
        user_id: str,
    ) -> None:
        """初始化采集编排。

        Args:
            document_root: 当前用户的工作区目录。
            lineage: 当前用户的版本链。
            roster: 所属课题组的成员表。
            store: 当前用户的信号存储。
            comparer: 版本比较器。
            user_id: 当前用户 ID，记为成员表的登记人。

        Returns:
            None。
        """
        self.document_root = document_root.resolve()  # 工作区目录。
        self.lineage = lineage  # 版本链。
        self.roster = roster  # 课题组成员表。
        self.store = store  # 信号存储。
        self.comparer = comparer  # 版本比较器。
        self.user_id = user_id  # 当前用户 ID。

    def observe(
        self,
        path: str,
        revision: str,
        *,
        previous_revision: str | None,
        session_id: str,
    ) -> None:
        """登记工具见到的文档版本，规则见模块说明。

        Args:
            path: 文档在工作区的相对路径。
            revision: 工具返回的文档 revision。
            previous_revision: 由 Seshat 修改得到时的修改前 revision，否则为 None。
            session_id: 当前会话 ID。

        Returns:
            None。

        Raises:
            OSError: 快照或版本记录写入失败。
            ValueError: 文档不是有效 DOCX。
        """
        if self.lineage.get(revision) is not None:
            return
        source = resolve_docx_path(self.document_root, path)
        if not self.lineage.save_snapshot(revision, source):
            return  # 文档在工具返回后又被修改，下次见到时再登记。
        version = self.lineage.load_version(revision)
        record = VersionRecord(
            revision=revision,
            paper_id="",
            parent_revision="",
            status="confirmed",
            origin="observed",
            path=source.relative_to(self.document_root).as_posix(),
            title=version.title,
            content_key=version.content_key,
            session_id=session_id,
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

        parent = self.lineage.get(previous_revision) if previous_revision else None
        if parent is None:
            parent = self.lineage.find_identical(version.content_key)
        else:
            record.origin = "written"
        if parent is not None:
            record.parent_revision = parent.revision
            record.paper_id = parent.paper_id
            self.lineage.add(record)
            return

        candidates = self.lineage.find_candidates(version, exclude_revision=revision)
        roles = self.roster.get_roles()
        unknown = [
            author
            for author in self.comparer.foreign_authors(version)
            if author not in roles
        ]
        if candidates or unknown:
            record.status = "pending"
            record.candidates = [
                {"revision": candidate.revision, "similarity": round(score, 3)}
                for candidate, score in candidates
            ]
            self.lineage.add(record)
            return

        self.lineage.add(record)
        self.store.append(
            self.comparer.compare(
                version,
                base=None,
                roles=roles,
                untracked_role=None,
                origin=SignalOrigin(record.path, revision, session_id),
            )
        )

    def confirm(
        self,
        path: str,
        *,
        base_revision: str | None,
        untracked_role: str | None,
        author_roles: Mapping[str, str],
    ) -> dict[str, Any]:
        """按用户确认的来源采集文档当前版本（或其待确认祖先）的修改信号。

        Args:
            path: 文档在工作区的路径。
            base_revision: 用户确认的历史版本 revision 或前缀；不是任何历史版本的新版时为 None。
            untracked_role: 未开修订的直接修改由谁完成；有基线时必填。
            author_roles: 本次登记的作者名到角色。

        Returns:
            ``ok``、``path``、``revision``（确认的版本）、``paper_id``（所属论文）、
            ``base_revision``（基线前缀或 None）、``signals``（本次新写入的信号按来源计数）、
            ``duplicates``（此前已采集过而跳过的信号数）。

        Raises:
            ValueError: 没有待确认的版本，或基线、角色、作者登记不完整。
            FileNotFoundError: 文档或快照不存在。
            OSError: 文件写入失败。
        """
        source = resolve_docx_path(self.document_root, path)
        relative_path = source.relative_to(self.document_root).as_posix()
        revision = calculate_revision(source)
        if self.lineage.get(revision) is None:
            self.observe(relative_path, revision, previous_revision=None, session_id="")
        target = self.lineage.pending_root(revision)
        if target is None:
            raise ValueError(f"{relative_path} 当前版本没有待确认的来源，无需调用")

        base = self.lineage.resolve(base_revision) if base_revision else None
        if base is not None:
            if base.status != "confirmed" or not base.paper_id:
                raise ValueError("基线版本的来源尚未确认，不能作为基线")
            if base.revision == target.revision:
                raise ValueError("基线版本不能是待确认的版本自身")
            if untracked_role not in SIGNAL_ROLES:
                raise ValueError(
                    "指定基线版本时必须说明未开修订的直接修改由谁完成："
                    f"untracked_role 取值 {', '.join(SIGNAL_ROLES)}"
                )
        for author, role in author_roles.items():
            if role not in AUTHOR_ROLES:
                raise ValueError(f"作者 {author} 的角色无效: {role}")

        version = self.lineage.load_version(target.revision)
        roles = {**self.roster.get_roles(), **author_roles}
        missing = [
            author
            for author in self.comparer.foreign_authors(version)
            if author not in roles
        ]
        if missing:
            raise ValueError(
                f"以下修订或批注作者的身份未登记，请先询问用户: {'、'.join(missing)}"
            )

        self.roster.update(author_roles, updated_by=self.user_id)
        signals = self.comparer.compare(
            version,
            base=self.lineage.load_version(base.revision) if base else None,
            roles=roles,
            untracked_role=untracked_role if base else None,
            origin=SignalOrigin(target.path, target.revision, target.session_id),
        )
        written = self.store.append(signals)
        confirmed = self.lineage.confirm(
            target.revision,
            parent_revision=base.revision if base else "",
        )
        return {
            "ok": True,
            "path": relative_path,
            "revision": confirmed.revision[:REVISION_PREFIX_LENGTH],
            "paper_id": confirmed.paper_id,
            "base_revision": (
                base.revision[:REVISION_PREFIX_LENGTH] if base is not None else None
            ),
            "signals": dict(Counter(signal.source for signal in written)),
            "duplicates": len(signals) - len(written),
        }

    def get_version_notice(self, revision: str) -> dict[str, Any] | None:
        """生成文档版本的待确认提示，供审阅状态展示。

        Args:
            revision: 文档当前 revision。

        Returns:
            待确认时为 ``path``、``revision``（前缀）、``candidates``（疑似历史版本：
            revision 前缀、path、title、seen_at 日期、similarity）、``unknown_authors``
            （未登记身份的作者）；无需确认时为 None。
        """
        target = self.lineage.pending_root(revision)
        if target is None:
            return None
        candidates: list[dict[str, Any]] = []
        for candidate in target.candidates:
            record = self.lineage.get(candidate["revision"])
            if record is not None:
                candidates.append(
                    {
                        "revision": record.revision[:REVISION_PREFIX_LENGTH],
                        "path": record.path,
                        "title": record.title,
                        "seen_at": record.created_at[:10],
                        "similarity": candidate["similarity"],
                    }
                )
        roles = self.roster.get_roles()
        version = self.lineage.load_version(target.revision)
        return {
            "path": target.path,
            "revision": target.revision[:REVISION_PREFIX_LENGTH],
            "candidates": candidates,
            "unknown_authors": [
                author
                for author in self.comparer.foreign_authors(version)
                if author not in roles
            ],
        }
