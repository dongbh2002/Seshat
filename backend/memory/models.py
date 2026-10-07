"""记忆的数据模型：四级记忆共用的条目结构、归属范围，以及供上下文与子任务读取记忆的接口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from backend.utils.time_id import new_time_id

MEMORY_LEVELS = ("document", "user", "tenant", "global")  # 文档、用户、课题组、通用。
MEMORY_KINDS = ("rule", "fact")  # 应遵循的规则、客观事实（如画像、文档约定）。
MEMORY_STATUSES = (  # 记忆状态。
    "active",  # 生效，注入上下文。
    "dormant",  # 长期未被支持而休眠，不注入；再次被支持时恢复生效。
    "candidate",  # 通用级候选，待人工审核。
    "promoted",  # 已晋升到上一级，由上级条目代替注入。
    "retired",  # 被推翻、被合并、被整理下线或被用户删除。
    "rejected",  # 通用级候选被审核拒绝，不再重复提出。
)
LIVE_STATUSES = ("active", "dormant")  # 仍可被强化、整理的状态。
MEMORY_SOURCES = (  # 记忆来源。
    "agent",  # 每轮结束后由记忆 agent 写入；课题组级即导师要求。
    "promoted",  # 会话结束时由多个下级范围的共性晋升；课题组级即课题组共性。
)
SYSTEM_AUTHOR = "system"  # 记忆 agent、晋升或整理写入的条目的写入者。


@dataclass(frozen=True)
class MemoryScope:
    """一份记忆文件的归属：级别与对应的课题组、用户、文档。"""

    level: str  # 记忆级别，取值见 MEMORY_LEVELS。
    tenant_id: str = ""  # 课题组 ID；通用级为空。
    user_id: str = ""  # 用户 ID；仅文档级与用户级有值。
    paper_id: str = ""  # 文档（论文）ID；仅文档级有值。


@dataclass
class MemoryItem:
    """一条记忆。

    支持分与反例分不单独存储，由各条依据实际计入的分数求和得出：同一依据只计一次，
    重试、合并、晋升都不会重复计分。
    """

    id: str  # 条目 ID：级别首字母加时间码，如 T-20261007-093000-a1b2c3。
    level: str  # 记忆级别，取值见 MEMORY_LEVELS。
    kind: str  # 记忆类型，取值见 MEMORY_KINDS。
    content: str  # 一句抽象陈述，不含论文原文。
    status: str  # 状态，取值见 MEMORY_STATUSES。
    source: str  # 来源，取值见 MEMORY_SOURCES。
    created_by: str  # 写入者：用户 ID 或 system。
    created_at: str  # 创建时间，ISO 8601。
    updated_at: str  # 最后修改时间，ISO 8601。
    last_supported_at: str  # 最近一次获得新支持依据（含创建）的时间，用于计算衰减。
    evidence: dict[str, int] = field(default_factory=dict)  # 支持依据 ID 到计入的分。
    counter_evidence: dict[str, int] = field(default_factory=dict)  # 反例依据到分。
    history: list[dict[str, str]] = field(default_factory=list)  # 修改历史。
    replaced_by: str = ""  # 晋升或合并后代替本条的条目 ID。

    @property
    def support(self) -> int:
        """支持分：各条支持依据计入分数之和。

        Returns:
            支持分。
        """
        return sum(self.evidence.values())

    @property
    def against(self) -> int:
        """反例分：各条反例依据计入分数之和；超过支持分时条目被推翻。

        Returns:
            反例分。
        """
        return sum(self.counter_evidence.values())

    @classmethod
    def create(
        cls,
        *,
        level: str,
        kind: str,
        content: str,
        status: str,
        source: str,
        created_by: str,
        evidence: Mapping[str, int],
    ) -> MemoryItem:
        """校验字段并生成新条目。

        Args:
            level: 记忆级别。
            kind: 记忆类型。
            content: 记忆内容。
            status: 初始状态。
            source: 来源。
            created_by: 写入者。
            evidence: 支持依据 ID 到计入的分；可为空，之后由 credit 计入。

        Returns:
            新条目。

        Raises:
            ValueError: 级别、类型、状态或来源无效，或内容为空。
        """
        for name, value, allowed in (
            ("级别", level, MEMORY_LEVELS),
            ("类型", kind, MEMORY_KINDS),
            ("状态", status, MEMORY_STATUSES),
            ("来源", source, MEMORY_SOURCES),
        ):
            if value not in allowed:
                raise ValueError(f"记忆{name}无效: {value}")
        if not content.strip():
            raise ValueError("记忆内容不能为空")
        now = _now()
        return cls(
            id=f"{level[0].upper()}-{new_time_id(datetime.now())}",
            level=level,
            kind=kind,
            content=" ".join(content.split()),
            status=status,
            source=source,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            last_supported_at=now,
            evidence=dict(evidence),
        )

    def credit(self, credits: Mapping[str, int]) -> int:
        """记入新的支持依据；已记录过的依据忽略。有新依据时刷新最近支持时间，休眠条目恢复生效。

        Args:
            credits: 依据 ID 到计入的分（调用方已按来源加权并封顶，可以为 0）。

        Returns:
            本次新增的支持分；没有新依据时为 0，且不刷新时间。
        """
        fresh = {
            key: value for key, value in credits.items() if key not in self.evidence
        }
        if not fresh:
            return 0
        self.evidence.update(fresh)
        self.last_supported_at = self.updated_at = _now()
        if self.status == "dormant":
            self.status = "active"
        return sum(fresh.values())

    def debit(self, credits: Mapping[str, int], *, history_limit: int) -> int:
        """记入新的反例依据；已记录过的依据忽略。反例分超过支持分时条目被推翻。

        Args:
            credits: 反例依据 ID 到计入的分（调用方已按来源加权）。
            history_limit: 修改历史保留条数。

        Returns:
            本次新增的反例分。
        """
        fresh = {
            key: value
            for key, value in credits.items()
            if key not in self.counter_evidence
        }
        if not fresh:
            return 0
        self.counter_evidence.update(fresh)
        self.updated_at = _now()
        if self.against > self.support:
            self.retire("反例分超过支持分", history_limit=history_limit)
        return sum(fresh.values())

    def change_content(self, content: str, *, reason: str, history_limit: int) -> None:
        """修改内容，旧内容记入历史；内容未变时不做任何改动。

        Args:
            content: 新内容。
            reason: 修改原因。
            history_limit: 修改历史保留条数。

        Returns:
            None。
        """
        normalized = " ".join(content.split())
        if normalized == self.content:
            return
        self._record(reason, history_limit)
        self.content = normalized

    def retire(self, reason: str, *, history_limit: int, replaced_by: str = "") -> None:
        """下线条目，原因记入历史。

        Args:
            reason: 下线原因。
            history_limit: 修改历史保留条数。
            replaced_by: 代替本条的条目 ID；没有时为空。

        Returns:
            None。
        """
        self._record(reason, history_limit)
        self.status = "retired"
        self.replaced_by = replaced_by

    def revive(self, reason: str, *, history_limit: int) -> None:
        """恢复为生效（如从归档恢复）：原状态记入历史，视为刚被支持。

        Args:
            reason: 恢复原因。
            history_limit: 修改历史保留条数。

        Returns:
            None。
        """
        self._record(reason, history_limit)
        self.status = "active"
        self.replaced_by = ""
        self.last_supported_at = self.updated_at

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的字典。

        Returns:
            与字段同名的键组成的字典。
        """
        return asdict(self)

    def _record(self, reason: str, history_limit: int) -> None:
        """把当前内容与状态连同变更原因记入历史，并更新修改时间。

        Args:
            reason: 变更原因。
            history_limit: 修改历史保留条数。

        Returns:
            None。
        """
        now = _now()
        self.history.append(
            {
                "content": self.content,
                "status": self.status,
                "changed_at": now,
                "reason": reason,
            }
        )
        del self.history[:-history_limit]
        self.updated_at = now


def find_live(items: list[MemoryItem], content: str) -> MemoryItem | None:
    """查找内容完全相同的生效或休眠条目，新增前用于改为强化。

    Args:
        items: 归属下的条目。
        content: 待新增的内容（已合并空白）。

    Returns:
        内容相同的条目；没有时为 None。
    """
    return next(
        (
            item
            for item in items
            if item.status in LIVE_STATUSES and item.content == content
        ),
        None,
    )


class MemorySource(Protocol):
    """为主对话上下文与审阅子任务提供当前会话可用的记忆。"""

    def select_memory(self) -> Mapping[str, Any]:
        """选取当前会话注入上下文的记忆。

        Returns:
            模板变量：``profile``（用户画像事实）、``documents``（本会话涉及文档的
            文档级记忆，每项含 title 与 entries）、``advisor``（课题组级导师要求）、
            ``tenant``（课题组共性）、``user``、``general``（通用），均为条目内容。
        """
        ...

    def rules_for_review(self, revision: str) -> list[dict[str, str]]:
        """返回审阅某个文档版本时应遵循的记忆，按“文档 > 导师要求 > 课题组共性 > 用户 > 通用”排列。

        Args:
            revision: 被审阅文档的 revision；只取该文档的文档级记忆。

        Returns:
            ``{"level", "content"}`` 列表，level 取 document、advisor、tenant、user、global。
        """
        ...


def _now() -> str:
    """返回当前时间的 ISO 8601 字符串。

    Returns:
        精确到秒、带时区的时间字符串。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")
