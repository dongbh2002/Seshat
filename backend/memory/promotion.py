"""记忆晋升（会话结束时运行）：多个下级范围中含义相同的规则归纳为上一级记忆。

    用户 → 课题组共性：同一规则出现在课题组内 promote_tenant_min_users 名学生中
    课题组 → 通用：同一规则出现在 promote_global_min_tenants 个课题组中，先作为候选，人工批准后生效
文档级记忆只关于单篇文档，不参与晋升。晋升生效后，下级条目标为 promoted，由上级条目代替注入上下文。
上级条目以成员条目 ID 为依据，每个成员计入其净支持分（支持分减反例分，不低于 0），新建与强化口径一致。
新建上级条目须达到上述范围数；已有上级条目（生效、休眠或候选）只需一个新范围即可强化，
强化生效条目时新成员同样标为 promoted（候选在批准时才标记）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.config import MemorySettings
from backend.llm_tasks import MemoryPromoter
from backend.memory.models import SYSTEM_AUTHOR, MemoryItem, MemoryScope
from backend.memory.store import MemoryStore


@dataclass(frozen=True)
class _Member:
    """参与归纳的一条下级规则。"""

    scope: MemoryScope  # 所在归属。
    item: MemoryItem  # 条目。


class MemoryPromotion:
    """执行用户→课题组共性的晋升，以及通用候选的生成与审核。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        promoter: MemoryPromoter,
        settings: MemorySettings,
    ) -> None:
        """初始化记忆晋升。

        Args:
            store: 记忆存储。
            promoter: 归纳规则的 LLM 子任务。
            settings: 记忆配置，提供晋升门槛与条目字符上限。

        Returns:
            None。
        """
        self.store = store  # 记忆存储。
        self.promoter = promoter  # 归纳规则的 LLM 子任务。
        self.settings = settings  # 记忆配置。

    def promote_users(self, tenant_id: str) -> int:
        """把课题组内多名学生相同的规则晋升到课题组级。

        Args:
            tenant_id: 课题组 ID。

        Returns:
            新增或强化的课题组级条目数。
        """
        return self._promote(
            self.store.user_scopes(tenant_id),
            MemoryScope("tenant", tenant_id),
            min_scopes=self.settings.promote_tenant_min_users,
        )

    def refresh_global_candidates(self) -> int:
        """从各课题组的规则中归纳通用候选，等待人工审核。

        Returns:
            新增或强化的候选数。
        """
        return self._promote(
            self.store.tenant_scopes(),
            MemoryScope("global"),
            min_scopes=self.settings.promote_global_min_tenants,
        )

    def global_candidates(self) -> list[MemoryItem]:
        """列出待审核的通用候选。

        Returns:
            按支持数从高到低排列的候选。
        """
        candidates = [
            item
            for item in self.store.load(MemoryScope("global"))
            if item.status == "candidate"
        ]
        return sorted(candidates, key=lambda item: item.support, reverse=True)

    def candidate_evidence(self, candidate: MemoryItem) -> list[tuple[str, MemoryItem]]:
        """列出通用候选所依据的课题组记忆，供审核时参考。

        Args:
            candidate: 通用候选。

        Returns:
            (课题组 ID, 课题组条目) 列表。
        """
        members = set(candidate.evidence)
        return [
            (scope.tenant_id, item)
            for scope in self.store.tenant_scopes()
            for item in self.store.load(scope)
            if item.id in members
        ]

    def approve(self, item_id: str) -> None:
        """批准通用候选：候选生效，作为证据的课题组条目标为已晋升。

        Args:
            item_id: 候选 ID。

        Returns:
            None。

        Raises:
            ValueError: 候选不存在。
        """
        approved = self._set_global_status(item_id, "active")
        members = set(approved.evidence)
        for scope in self.store.tenant_scopes():
            self.store.update(
                scope, lambda items: _mark_promoted(items, members, approved.id)
            )

    def reject(self, item_id: str) -> None:
        """拒绝通用候选；被拒绝的候选保留，避免再次提出。

        Args:
            item_id: 候选 ID。

        Returns:
            None。

        Raises:
            ValueError: 候选不存在。
        """
        self._set_global_status(item_id, "rejected")

    def _set_global_status(self, item_id: str, status: str) -> MemoryItem:
        """修改通用候选的状态。

        Args:
            item_id: 候选 ID。
            status: 新状态。

        Returns:
            修改后的条目。

        Raises:
            ValueError: 候选不存在。
        """
        found: list[MemoryItem] = []

        def change(items: list[MemoryItem]) -> None:
            for item in items:
                if item.id == item_id and item.status == "candidate":
                    item.status = status
                    found.append(item)

        self.store.update(MemoryScope("global"), change)
        if not found:
            raise ValueError(f"通用候选不存在: {item_id}")
        return found[0]

    def _promote(
        self,
        lower_scopes: Sequence[MemoryScope],
        upper: MemoryScope,
        *,
        min_scopes: int,
    ) -> int:
        """把多个下级归属中含义相同的规则归纳到上级归属。

        Args:
            lower_scopes: 下级归属。
            upper: 上级归属。
            min_scopes: 一条规则至少出现在多少个下级归属中才能晋升。

        Returns:
            新增或强化的上级条目数。
        """
        members = [
            _Member(scope, item)
            for scope in lower_scopes
            for item in self.store.load(scope)
            if item.status == "active" and item.kind == "rule"
        ]
        existing = {
            item.id: item
            for item in self.store.load(upper)
            if item.status in {"active", "dormant", "candidate", "rejected"}
        }
        reinforceable = any(
            item.status != "rejected" for item in existing.values()
        )  # 有可强化的上级条目时，单个范围的新成员也值得归纳。
        if not members or (
            len({member.scope for member in members}) < min_scopes and not reinforceable
        ):
            return 0
        scope_labels = {
            scope: _scope_label(index)
            for index, scope in enumerate(dict.fromkeys(m.scope for m in members))
        }
        labels = {f"R{index}": member for index, member in enumerate(members, start=1)}
        candidate_level = upper.level == "global"
        groups = self.promoter.group(
            target_level=upper.level,
            items=[
                {
                    "label": label,
                    "scope": scope_labels[member.scope],
                    "content": member.item.content,
                }
                for label, member in labels.items()
            ],
            existing=[
                {"id": item.id, "status": item.status, "content": item.content}
                for item in existing.values()
            ],
            item_max_chars=self.settings.item_max_chars,
        )

        by_content = {item.content: item for item in existing.values()}
        covered = {
            member_id for item in existing.values() for member_id in item.evidence
        }
        additions: list[MemoryItem] = []
        reinforcements: dict[str, dict[str, int]] = {}  # 上级条目 ID 到成员依据。
        promoted: dict[MemoryScope, dict[str, str]] = {}  # 被晋升的下级条目。
        for group in groups:
            grouped = [
                labels[label] for label in group.get("members", []) if label in labels
            ]
            if not grouped:
                continue
            evidence = {
                member.item.id: max(0, member.item.support - member.item.against)
                for member in grouped
            }
            content = group.get("content")
            # 模型未指明目标时按内容相同兜底；成员都已是某个上级条目
            # （含被拒绝的候选）的证据时，不再重复提出。
            target = existing.get(str(group.get("target"))) or (
                by_content.get(" ".join(content.split()))
                if isinstance(content, str)
                else None
            )
            if target is not None:
                if target.status == "rejected":
                    continue
                reinforcements.setdefault(target.id, {}).update(evidence)
                upper_id = target.id
                mark = not candidate_level or target.status == "active"
            else:
                if (
                    len({member.scope for member in grouped}) < min_scopes
                    or covered.issuperset(evidence)
                    or not isinstance(content, str)
                    or not content.strip()
                ):
                    continue
                item = MemoryItem.create(
                    level=upper.level,
                    kind="rule",
                    content=content[: self.settings.item_max_chars],
                    status="candidate" if candidate_level else "active",
                    source="promoted",
                    created_by=SYSTEM_AUTHOR,
                    evidence=evidence,
                )
                additions.append(item)
                upper_id = item.id
                mark = not candidate_level
            if mark:
                for member in grouped:
                    promoted.setdefault(member.scope, {})[member.item.id] = upper_id

        if not additions and not reinforcements:
            return 0

        def change_upper(items: list[MemoryItem]) -> None:
            for item in items:
                if item.id in reinforcements:
                    item.credit(reinforcements[item.id])
            items.extend(additions)

        self.store.update(upper, change_upper)
        for scope, mapping in promoted.items():
            self.store.update(
                scope, lambda items, mapping=mapping: _apply_promotion(items, mapping)
            )
        return len(additions) + len(reinforcements)


def _scope_label(index: int) -> str:
    """生成匿名的范围代号，避免把学生或课题组身份交给模型。

    Args:
        index: 范围序号，从 0 开始。

    Returns:
        A、B、…、Z、A1、B1…
    """
    letter = chr(ord("A") + index % 26)
    return letter if index < 26 else f"{letter}{index // 26}"


def _apply_promotion(items: list[MemoryItem], mapping: dict[str, str]) -> None:
    """把已晋升的下级条目标为 promoted。

    Args:
        items: 下级归属的条目（原地修改）。
        mapping: 条目 ID 到上级条目 ID。

    Returns:
        None。
    """
    for item in items:
        if item.id in mapping and item.status == "active":
            item.status = "promoted"
            item.replaced_by = mapping[item.id]


def _mark_promoted(
    items: list[MemoryItem], member_ids: set[str], upper_id: str
) -> None:
    """把作为通用条目证据的课题组条目标为 promoted。

    Args:
        items: 课题组归属的条目（原地修改）。
        member_ids: 作为证据的条目 ID。
        upper_id: 通用条目 ID。

    Returns:
        None。
    """
    _apply_promotion(
        items, dict.fromkeys(member_ids & {item.id for item in items}, upper_id)
    )
