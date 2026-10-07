"""修改信号的数据模型：各来源的修改与反馈统一表示为 Signal，供记忆沉淀消费。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

SIGNAL_SOURCES = (  # 信号来源。
    "finding_outcome",  # Seshat 提出的问题被用户接受、拒绝或修改。
    "decision",  # 用户在对话中明确的审阅决定或偏好。
    "tracked_revision",  # 文档中他人带作者的修订。
    "comment",  # 文档中他人的批注。
    "untracked_edit",  # 前后两版之间未开修订的直接修改。
    "assistant_revision",  # Seshat 写入的修订在新版本中的去向。
)
AUTHOR_ROLES = ("advisor", "student", "other")  # 成员表中作者的角色：导师、学生、其他。
SIGNAL_ROLES = (*AUTHOR_ROLES, "unknown")  # 信号中修改者的角色，无法确认时为 unknown。
SIGNAL_OUTCOMES = (  # 建议类信号的结局。
    "accepted",  # 被接受。
    "rejected",  # 被拒绝。
    "resolved",  # 已按问题修改。
    "rewritten",  # 被人改写为第三种文本。
)
_ID_LENGTH = 16  # 信号 ID 保留的十六进制位数。


@dataclass(frozen=True)
class Signal:
    """一条修改信号：谁、对哪段原文、改成了什么或提了什么意见、结局如何。"""

    id: str  # 按内容计算的去重键，同一修改重复采集时相同。
    source: str  # 信号来源，取值见 SIGNAL_SOURCES。
    actor: str  # 修改者：Word 作者名或用户 ID；未开修订的修改无法得知时为空。
    role: str  # 修改者角色，取值见 SIGNAL_ROLES。
    document: str  # 文档在工作区的相对路径（采集时）。
    revision: str  # 信号所在文档版本的 SHA-256。
    block_id: str  # 内容块 ID；与块无关时为空。
    section: str  # 内容块所在标题路径，如“4 实验 > 4.2 结果”。
    before: str  # 修改前原文；新增内容为空。
    after: str  # 修改后原文；批注、决定、未修改的结局为空。
    comment: str  # 批注内容、问题描述或决定原文。
    suggestion: str  # Seshat 当时建议的修改后文本，仅 assistant_revision。
    outcome: str  # 建议类信号的结局，取值见 SIGNAL_OUTCOMES；其余为空。
    occurred_at: str  # 修改发生时间（修订或批注的 w:date）；未知为空。
    session_id: str  # 采集时所在会话 ID；未知为空。
    created_at: str  # 采集时间，ISO 8601。

    @classmethod
    def create(
        cls,
        *,
        source: str,
        actor: str,
        role: str,
        document: str,
        revision: str,
        session_id: str,
        block_id: str = "",
        section: str = "",
        before: str = "",
        after: str = "",
        comment: str = "",
        suggestion: str = "",
        outcome: str = "",
        occurred_at: str = "",
    ) -> Signal:
        """校验字段并生成带去重 ID 与采集时间的信号。

        Args:
            source: 信号来源。
            actor: 修改者。
            role: 修改者角色。
            document: 文档相对路径。
            revision: 文档版本。
            session_id: 会话 ID。
            block_id: 内容块 ID。
            section: 标题路径。
            before: 修改前原文。
            after: 修改后原文。
            comment: 批注、问题描述或决定原文。
            suggestion: Seshat 当时建议的修改后文本。
            outcome: 结局。
            occurred_at: 修改发生时间。

        Returns:
            新信号。

        Raises:
            ValueError: 来源、角色或结局取值无效。
        """
        if source not in SIGNAL_SOURCES:
            raise ValueError(f"信号来源无效: {source}")
        if role not in SIGNAL_ROLES:
            raise ValueError(f"信号角色无效: {role}")
        if outcome and outcome not in SIGNAL_OUTCOMES:
            raise ValueError(f"信号结局无效: {outcome}")
        # 路径、版本、块 ID 与会话会随版本变化，不参与去重。
        content = [
            source,
            actor,
            before,
            after,
            comment,
            suggestion,
            outcome,
            occurred_at,
        ]
        digest = hashlib.sha256(
            json.dumps(content, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return cls(
            id=digest[:_ID_LENGTH],
            source=source,
            actor=actor,
            role=role,
            document=document,
            revision=revision,
            block_id=block_id,
            section=section,
            before=before,
            after=after,
            comment=comment,
            suggestion=suggestion,
            outcome=outcome,
            occurred_at=occurred_at,
            session_id=session_id,
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的字典。

        Returns:
            与字段同名的键组成的字典。
        """
        return asdict(self)
