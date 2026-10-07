"""记忆维护（会话结束时运行，管理员也可全量运行）：休眠标记、模型整理与归档治理。

休眠：得分低于 dormant_score 的用户级、课题组级生效条目转为休眠。
整理：只把本会话变动的条目及其最相似的条目交给模型，分块进行，单次不超过 consolidate_max_items；
      全量运行时以全部条目为变动条目。
归档：已下线、已晋升、得分低于 archive_score 的休眠条目，以及主文件超出容量时得分最低的条目
      （先休眠后生效）移入归档；通用级只归档已下线与已晋升的条目，被拒绝的候选留在主文件防止重复提出。

整理的代码约束：
    每条记忆最多参与一个操作；
    合并只能发生在类型与来源都相同的条目之间，新条目取各成员支持依据与反例依据的并集（同一依据只计一次），
    被合并的条目下线并指向新条目；
    下线与改写必须给出原因；所有变更记入条目的修改历史，可追溯。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from backend.config import MemorySettings
from backend.llm_tasks import MemoryConsolidator
from backend.memory.models import LIVE_STATUSES, SYSTEM_AUTHOR, MemoryItem, MemoryScope
from backend.memory.scoring import MemoryScorer
from backend.memory.store import MemoryStore
from backend.utils.text import jaccard

_NEIGHBOR_NGRAM = 2  # 计算条目相似度时的字符片段长度。
_DEAD_REASONS = {  # 直接归档的状态到归档原因。
    "retired": "已下线",
    "promoted": "已晋升",
}
_LOGGER = logging.getLogger(__name__)


class MemoryMaintenance:
    """对一个归属执行休眠标记与模型整理。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        scorer: MemoryScorer,
        consolidator: MemoryConsolidator,
        settings: MemorySettings,
    ) -> None:
        """初始化记忆维护。

        Args:
            store: 记忆存储。
            scorer: 记忆评分，判断是否应当休眠。
            consolidator: 记忆整理的 LLM 子任务。
            settings: 记忆配置。

        Returns:
            None。
        """
        self.store = store  # 记忆存储。
        self.scorer = scorer  # 记忆评分。
        self.consolidator = consolidator  # 记忆整理子任务。
        self.settings = settings  # 记忆配置。

    def mark_dormant(self, scope: MemoryScope) -> int:
        """把得分低于休眠阈值的生效条目转为休眠。

        Args:
            scope: 记忆归属。

        Returns:
            转为休眠的条目数。
        """
        now = datetime.now().astimezone()
        if not any(
            item.status == "active" and self.scorer.is_dormant(item, now)
            for item in self.store.load(scope)
        ):
            return 0
        marked: list[str] = []

        def change(items: list[MemoryItem]) -> None:
            for item in items:
                if item.status == "active" and self.scorer.is_dormant(item, now):
                    item.status = "dormant"
                    marked.append(item.id)

        self.store.update(scope, change)
        return len(marked)

    def consolidate(self, scope: MemoryScope, *, since: datetime | None) -> int:
        """由模型整理归属：变动条目连同其最相似的条目分块交给模型。

        归属内生效与休眠条目不足 consolidate_min_items 时不整理。

        Args:
            scope: 用户级或课题组级归属。
            since: 只整理此时间之后修改过的条目；None 表示全部条目（全量整理）。

        Returns:
            执行的整理操作数。
        """
        live = self._live(scope)
        if len(live) < self.settings.consolidate_min_items:
            return 0
        focus = [
            item.id
            for item in live
            if since is None or datetime.fromisoformat(item.updated_at) >= since
        ]
        chunk = max(1, self.settings.consolidate_max_items // 2)
        total = 0
        while focus:
            live_by_id = {item.id: item for item in live}
            current = [live_by_id[item] for item in focus[:chunk] if item in live_by_id]
            focus = focus[chunk:]
            if not current:
                continue
            group = self._with_neighbors(current, live)
            operations = self.consolidator.consolidate(
                level=scope.level,
                items=[
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "source": item.source,
                        "support": item.support,
                        "against": item.against,
                        "last_supported": item.last_supported_at[:10],
                        "dormant": item.status == "dormant",
                        "content": item.content,
                    }
                    for item in group
                ],
                item_max_chars=self.settings.item_max_chars,
            )
            changes = self._validate(operations, {item.id: item for item in group})
            if changes:
                limit = self.settings.history_max_entries
                self.store.update(
                    scope, lambda items, changes=changes: _apply(items, changes, limit)
                )
                total += len(changes)
                live = self._live(scope)
        return total

    def archive(self, scope: MemoryScope) -> int:
        """把已下线、已晋升、长期休眠与超出容量的条目移入归档，规则见模块说明。

        Args:
            scope: 记忆归属。

        Returns:
            移入归档的条目数。
        """
        now = datetime.now().astimezone()
        capacity = {
            "document": self.settings.document_capacity,
            "user": self.settings.user_capacity,
            "tenant": self.settings.tenant_capacity,
        }.get(scope.level)

        def choose(items: list[MemoryItem]) -> dict[str, str]:
            reasons = {
                item.id: _DEAD_REASONS[item.status]
                for item in items
                if item.status in _DEAD_REASONS
            }
            for item in items:
                if (
                    item.status == "dormant"
                    and self.scorer.score(item, now) < self.settings.archive_score
                ):
                    reasons[item.id] = "长期休眠"
            if capacity is not None:
                kept = [
                    item
                    for item in items
                    if item.status in LIVE_STATUSES and item.id not in reasons
                ]
                kept.sort(
                    key=lambda item: (
                        item.status != "dormant",
                        self.scorer.score(item, now),
                        item.last_supported_at,
                    )
                )
                for item in kept[: max(0, len(kept) - capacity)]:
                    reasons[item.id] = "超出容量"
            return reasons

        return len(self.store.archive(scope, choose))

    def run_full(self) -> dict[str, int]:
        """全量维护全部课题组、用户与文档：休眠标记、全量整理与归档（管理员手动运行）。

        Returns:
            ``dormant``、``consolidated``、``archived`` 三项计数。
        """
        counts = {"dormant": 0, "consolidated": 0, "archived": 0}
        for tenant_id in self.store.tenant_ids():
            scopes = [MemoryScope("tenant", tenant_id)]
            for user_id in self.store.user_ids(tenant_id):
                scopes.append(MemoryScope("user", tenant_id, user_id))
                scopes.extend(self.store.document_scopes(tenant_id, user_id))
            for scope in scopes:
                if scope.level != "document":
                    counts["dormant"] += self.mark_dormant(scope)
                    counts["consolidated"] += self.consolidate(scope, since=None)
                counts["archived"] += self.archive(scope)
        counts["archived"] += self.archive(MemoryScope("global"))
        return counts

    def _live(self, scope: MemoryScope) -> list[MemoryItem]:
        """读取归属下的生效与休眠条目。

        Args:
            scope: 记忆归属。

        Returns:
            条目列表。
        """
        return [item for item in self.store.load(scope) if item.status in LIVE_STATUSES]

    def _with_neighbors(
        self,
        focus: Sequence[MemoryItem],
        live: Sequence[MemoryItem],
    ) -> list[MemoryItem]:
        """为变动条目附上最相似的条目，总数不超过 consolidate_max_items。

        Args:
            focus: 变动条目。
            live: 归属下的全部生效与休眠条目。

        Returns:
            变动条目在前、相似条目在后的去重列表。
        """
        chosen = {item.id: item for item in focus}
        others = [item for item in live if item.id not in chosen]
        for item in focus:
            ranked = sorted(
                others,
                key=lambda other: jaccard(item.content, other.content, _NEIGHBOR_NGRAM),
                reverse=True,
            )
            for neighbor in ranked[: self.settings.consolidate_neighbors]:
                chosen.setdefault(neighbor.id, neighbor)
        return list(chosen.values())[: self.settings.consolidate_max_items]

    def _validate(
        self,
        operations: Sequence[Mapping[str, Any]],
        live: Mapping[str, MemoryItem],
    ) -> list[dict[str, Any]]:
        """校验整理操作，约束见模块说明。

        Args:
            operations: 模型返回的操作。
            live: 参与整理的条目 ID 到条目。

        Returns:
            合法的变更：merge（ids、content）、retire（id、reason）、rewrite（id、content、reason）。
        """
        used: set[str] = set()
        changes: list[dict[str, Any]] = []
        limit = self.settings.item_max_chars
        for operation in operations:
            op = operation.get("op")
            content = operation.get("content")
            reason = operation.get("reason")
            if op == "merge":
                ids = list(
                    dict.fromkeys(str(item) for item in operation.get("ids", []))
                )
                members = [
                    live[item] for item in ids if item in live and item not in used
                ]
                valid = (
                    len(members) == len(ids) >= 2
                    and len({(item.kind, item.source) for item in members}) == 1
                    and isinstance(content, str)
                    and content.strip()
                )
                if valid:
                    changes.append({"op": op, "ids": ids, "content": content[:limit]})
                    used.update(ids)
                    continue
            elif op in {"retire", "rewrite"}:
                item_id = str(operation.get("id"))
                valid = (
                    item_id in live
                    and item_id not in used
                    and isinstance(reason, str)
                    and reason.strip()
                    and (
                        op == "retire" or (isinstance(content, str) and content.strip())
                    )
                )
                if valid:
                    changes.append(
                        {
                            "op": op,
                            "id": item_id,
                            "reason": reason.strip(),
                            "content": content[:limit] if op == "rewrite" else "",
                        }
                    )
                    used.add(item_id)
                    continue
            _LOGGER.warning("记忆整理操作无效，已忽略: %s", operation)
        return changes


def _union(evidence_maps: Iterable[Mapping[str, int]]) -> dict[str, int]:
    """合并多份依据记录，同一依据只保留一份（取较高的计入分）。

    Args:
        evidence_maps: 依据 ID 到计入分的多份记录。

    Returns:
        合并后的依据记录。
    """
    merged: dict[str, int] = {}
    for evidence in evidence_maps:
        for evidence_id, points in evidence.items():
            merged[evidence_id] = max(merged.get(evidence_id, 0), points)
    return merged


def _apply(
    items: list[MemoryItem],
    changes: Sequence[Mapping[str, Any]],
    history_limit: int,
) -> None:
    """把整理变更应用到归属的条目列表；执行时条目已不再生效或休眠的变更跳过。

    Args:
        items: 归属下的条目（原地修改）。
        changes: ``MemoryMaintenance._validate`` 生成的变更。
        history_limit: 修改历史保留条数。

    Returns:
        None。
    """
    live = {item.id: item for item in items if item.status in LIVE_STATUSES}
    for change in changes:
        if change["op"] == "merge":
            members = [live[item] for item in change["ids"] if item in live]
            if len(members) < 2:
                continue
            merged = MemoryItem.create(
                level=members[0].level,
                kind=members[0].kind,
                content=change["content"],
                status=(
                    "active"
                    if any(item.status == "active" for item in members)
                    else "dormant"
                ),
                source=members[0].source,
                created_by=SYSTEM_AUTHOR,
                evidence=_union(item.evidence for item in members),
            )
            merged.counter_evidence = _union(item.counter_evidence for item in members)
            merged.last_supported_at = max(item.last_supported_at for item in members)
            merged.history = [
                {
                    "content": item.content,
                    "status": item.status,
                    "changed_at": merged.created_at,
                    "reason": f"合并自 {item.id}",
                }
                for item in members
            ][-history_limit:]
            for item in members:
                item.retire(
                    f"整理合并到 {merged.id}",
                    history_limit=history_limit,
                    replaced_by=merged.id,
                )
                del live[item.id]
            items.append(merged)
            continue
        item = live.get(change["id"])
        if item is None:
            continue
        if change["op"] == "retire":
            item.retire(f"整理下线：{change['reason']}", history_limit=history_limit)
            del live[item.id]
        else:
            item.change_content(
                change["content"],
                reason=f"整理改写：{change['reason']}",
                history_limit=history_limit,
            )
