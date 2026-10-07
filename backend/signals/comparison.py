"""版本比较：从一个文档版本（及其基线版本）中提取修改信号。

三层信号：
    1. 新版本中他人的修订与批注（带作者）           → tracked_revision / comment
    2. 基线中 Seshat 写入的修订在新版本中的去向      → assistant_revision（接受 / 拒绝 / 被改写）
    3. 两版拒绝全部修订后的差异，去掉第 2 层已解释的部分 → untracked_edit
第 2、3 层需要基线版本；第 2 层先于第 3 层判断，避免把“用户接受了 Seshat 的修订”算作他人的修改。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from backend.signals.document import REJECT_ALL, DocxVersion
from backend.signals.models import Signal
from backend.utils.docx import DocumentBlock, align_blocks


@dataclass(frozen=True)
class SignalOrigin:
    """信号所属的文档版本与会话。"""

    document: str  # 文档在工作区的相对路径。
    revision: str  # 新版本的 revision。
    session_id: str  # 新版本首次被见到时的会话 ID。


class VersionComparer:
    """比较文档版本并生成修改信号。"""

    def __init__(self, *, assistant_author: str, block_match_threshold: float) -> None:
        """初始化版本比较器。

        Args:
            assistant_author: Seshat 写入修订与批注时使用的作者名。
            block_match_threshold: 两版间按文本相似度配对内容块的最低相似度。

        Returns:
            None。
        """
        self.assistant_author = assistant_author  # Seshat 的修订作者名。
        self.block_match_threshold = block_match_threshold  # 内容块配对的最低相似度。

    def foreign_authors(self, version: DocxVersion) -> list[str]:
        """列出版本中除 Seshat 外的修订与批注作者。

        Args:
            version: 文档版本。

        Returns:
            按首次出现顺序去重的作者名，不含空作者名。
        """
        authors = dict.fromkeys([*version.revision_authors, *version.comment_authors])
        return [
            author for author in authors if author and author != self.assistant_author
        ]

    def compare(
        self,
        version: DocxVersion,
        *,
        base: DocxVersion | None,
        roles: Mapping[str, str],
        untracked_role: str | None,
        origin: SignalOrigin,
    ) -> list[Signal]:
        """生成版本的修改信号。

        Args:
            version: 新版本。
            base: 用户确认的基线版本；没有时只生成第 1 层信号。
            roles: 作者名到角色的映射，未登记的作者记为 unknown。
            untracked_role: 未开修订的直接修改由谁完成；有基线时必填。
            origin: 信号所属的文档版本与会话。

        Returns:
            修改信号列表。

        Raises:
            ValueError: 有基线但未提供 untracked_role。
        """
        signals = self._tracked_signals(version, roles, origin)
        if base is not None:
            if untracked_role is None:
                raise ValueError("比较基线版本时必须提供 untracked_role")
            signals.extend(self._base_signals(version, base, untracked_role, origin))
        return signals

    def _tracked_signals(
        self,
        version: DocxVersion,
        roles: Mapping[str, str],
        origin: SignalOrigin,
    ) -> list[Signal]:
        """第 1 层：新版本中他人的修订（按作者分别还原）与批注。

        Args:
            version: 新版本。
            roles: 作者名到角色的映射。
            origin: 信号所属的文档版本与会话。

        Returns:
            tracked_revision 与 comment 信号。
        """
        rejected = version.view(REJECT_ALL)
        signals: list[Signal] = []
        for block in version.blocks:
            section = version.heading_paths[block.block_id]
            for revision in block.revisions:
                author = revision["author"]
                if author == self.assistant_author:
                    continue
                before = rejected[block.block_id]
                after = version.view(frozenset({author}))[block.block_id]
                if _same(before, after):
                    continue
                signals.append(
                    Signal.create(
                        source="tracked_revision",
                        actor=author,
                        role=roles.get(author, "unknown"),
                        document=origin.document,
                        revision=origin.revision,
                        session_id=origin.session_id,
                        block_id=block.block_id,
                        section=section,
                        before=before,
                        after=after,
                        occurred_at=revision["date"],
                    )
                )
            for comment in block.comments:
                author = comment["author"]
                if author == self.assistant_author:
                    continue
                signals.append(
                    Signal.create(
                        source="comment",
                        actor=author,
                        role=roles.get(author, "unknown"),
                        document=origin.document,
                        revision=origin.revision,
                        session_id=origin.session_id,
                        block_id=block.block_id,
                        section=section,
                        before=block.text,
                        comment=comment["text"],
                        occurred_at=comment["date"],
                    )
                )
        return signals

    def _base_signals(
        self,
        version: DocxVersion,
        base: DocxVersion,
        untracked_role: str,
        origin: SignalOrigin,
    ) -> list[Signal]:
        """第 2、3 层：对齐两版拒绝全部修订后的正文，判断 Seshat 修订的去向与直接修改。

        Args:
            version: 新版本。
            base: 基线版本。
            untracked_role: 未开修订的直接修改由谁完成。
            origin: 信号所属的文档版本与会话。

        Returns:
            assistant_revision 与 untracked_edit 信号。
        """
        old_texts = base.view(REJECT_ALL)
        suggested = base.view(frozenset({self.assistant_author}))
        new_texts = version.view(REJECT_ALL)
        old_blocks = {block.block_id: block for block in base.blocks}
        new_blocks = {block.block_id: block for block in version.blocks}
        pairs = align_blocks(
            [(block.block_id, old_texts[block.block_id]) for block in base.blocks],
            [(block.block_id, new_texts[block.block_id]) for block in version.blocks],
            min_similarity=self.block_match_threshold,
        )

        signals: list[Signal] = []
        for pair in pairs:
            old_block = old_blocks.get(pair.old_id) if pair.old_id else None
            new_block = new_blocks.get(pair.new_id) if pair.new_id else None
            before = old_texts[pair.old_id] if pair.old_id else ""
            after = new_texts[pair.new_id] if pair.new_id else ""
            block_id = pair.new_id or pair.old_id or ""
            section = (
                version.heading_paths[pair.new_id]
                if pair.new_id
                else base.heading_paths.get(block_id, "")
            )
            settled = (
                old_block is not None
                and self._has_assistant_revision(old_block)
                and not (new_block and self._has_assistant_revision(new_block))
            )
            if settled and pair.old_id:
                suggestion = suggested[pair.old_id]
                outcome = (
                    "accepted"
                    if _same(after, suggestion)
                    else "rejected"
                    if _same(after, before)
                    else "rewritten"
                )
                signals.append(
                    Signal.create(
                        source="assistant_revision",
                        actor="",
                        role=untracked_role,
                        document=origin.document,
                        revision=origin.revision,
                        session_id=origin.session_id,
                        block_id=block_id,
                        section=section,
                        before=before,
                        after=after,
                        suggestion=suggestion,
                        outcome=outcome,
                    )
                )
            elif not _same(before, after):
                signals.append(
                    Signal.create(
                        source="untracked_edit",
                        actor="",
                        role=untracked_role,
                        document=origin.document,
                        revision=origin.revision,
                        session_id=origin.session_id,
                        block_id=block_id,
                        section=section,
                        before=before,
                        after=after,
                    )
                )
        # TODO: Seshat 写入的批注在新版本中被删除或回复，也可作为建议结局。
        return signals

    def _has_assistant_revision(self, block: DocumentBlock) -> bool:
        """判断内容块是否含 Seshat 写入的修订。

        Args:
            block: 内容块。

        Returns:
            含 Seshat 修订时为 True。
        """
        return any(
            revision["author"] == self.assistant_author for revision in block.revisions
        )


def _same(first: str, second: str) -> bool:
    """忽略空白差异比较两段正文。

    Args:
        first: 第一段正文。
        second: 第二段正文。

    Returns:
        合并空白后相同时为 True。
    """
    return first.split() == second.split()
