"""用户查看、修改、删除与恢复记忆，查看记忆统计。写入由记忆 agent、晋升与整理负责；通用级只读，由管理员审核。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.config import MemorySettings
from backend.memory.models import LIVE_STATUSES, MemoryItem, MemoryScope
from backend.memory.store import ArchivedMemory, MemoryStore
from backend.signals import DocumentLineage, Signal, SignalStore

EDITABLE_LEVELS = ("document", "user", "tenant")  # 用户可以修改和删除的级别。


@dataclass(frozen=True)
class VisibleMemory:
    """用户可见的一条记忆及其范围说明。"""

    scope: MemoryScope  # 记忆归属。
    label: str  # 范围说明，如“文档《…》”“导师要求”。
    item: MemoryItem  # 条目。


@dataclass(frozen=True)
class MemoryStats:
    """一个级别的记忆统计。"""

    label: str  # 级别说明，文档级含文档篇数。
    counts: dict[str, int]  # 主文件中各状态的条目数。
    archived: int  # 归档记录数。
    size_bytes: int  # 主文件与归档文件的总字节数。


class MemoryCurator:
    """为当前用户列出、查看、修改、删除与恢复记忆，并统计记忆规模。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        lineage: DocumentLineage,
        signals: SignalStore,
        settings: MemorySettings,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """初始化记忆管理。

        Args:
            store: 记忆存储。
            lineage: 版本链，用于显示文档标题。
            signals: 当前用户的信号存储，用于显示记忆依据。
            settings: 记忆配置，提供条目字符上限与历史保留条数。
            tenant_id: 当前课题组 ID。
            user_id: 当前用户 ID。

        Returns:
            None。
        """
        self.store = store  # 记忆存储。
        self.lineage = lineage  # 版本链。
        self.signals = signals  # 当前用户的信号存储。
        self.settings = settings  # 记忆配置。
        self.tenant_id = tenant_id  # 当前课题组 ID。
        self.user_id = user_id  # 当前用户 ID。

    def visible(self) -> list[VisibleMemory]:
        """列出当前用户可见的生效与休眠记忆：本人全部文档、用户级、所在课题组与通用级。

        Returns:
            按级别排列的可见记忆。
        """
        entries: list[VisibleMemory] = []
        for scope in self.store.document_scopes(self.tenant_id, self.user_id):
            label = f"文档《{self.lineage.paper_title(scope.paper_id)}》"
            entries.extend(self._live(scope, lambda _: label))
        entries.extend(
            self._live(
                MemoryScope("user", self.tenant_id, self.user_id), lambda _: "用户"
            )
        )
        entries.extend(
            self._live(
                MemoryScope("tenant", self.tenant_id),
                lambda item: "导师要求" if item.source == "agent" else "课题组共性",
            )
        )
        entries.extend(self._live(MemoryScope("global"), lambda _: "通用"))
        return entries

    def details(
        self,
        item_id: str,
    ) -> tuple[VisibleMemory, list[Signal], list[Signal]]:
        """查看一条可见记忆及其中属于本人的支持依据与反例依据信号。

        他人信号形成的依据（如课题组共性）只计数、不展示原文。

        Args:
            item_id: 条目 ID。

        Returns:
            (可见记忆, 本人的支持依据信号, 本人的反例依据信号)。

        Raises:
            ValueError: 条目不存在或不可见。
        """
        entry = self._find(item_id)
        found = self.signals.find(
            set(entry.item.evidence) | set(entry.item.counter_evidence)
        )
        return (
            entry,
            [signal for signal in found if signal.id in entry.item.evidence],
            [signal for signal in found if signal.id in entry.item.counter_evidence],
        )

    def edit(self, item_id: str, content: str) -> MemoryItem:
        """修改一条本人可见的记忆内容，旧内容记入历史；通用级不能修改。

        Args:
            item_id: 条目 ID。
            content: 新内容。

        Returns:
            修改后的条目。

        Raises:
            ValueError: 条目不存在、不可见、属于通用级，或内容为空、超长。
        """
        if not content.strip():
            raise ValueError("记忆内容不能为空")
        if len(content.strip()) > self.settings.item_max_chars:
            raise ValueError(f"记忆内容超过 {self.settings.item_max_chars} 字")
        return self._change(
            item_id,
            lambda item: item.change_content(
                content,
                reason=f"{self.user_id} 修改",
                history_limit=self.settings.history_max_entries,
            ),
        )

    def forget(self, item_id: str) -> MemoryItem:
        """删除（下线）一条本人可见的记忆，原因记入历史；通用级不能删除。

        Args:
            item_id: 条目 ID。

        Returns:
            被下线的条目。

        Raises:
            ValueError: 条目不存在、不可见或属于通用级。
        """
        return self._change(
            item_id,
            lambda item: item.retire(
                f"{self.user_id} 删除",
                history_limit=self.settings.history_max_entries,
            ),
        )

    def archived(self) -> list[tuple[str, ArchivedMemory]]:
        """列出本人可恢复的归档记忆：本人全部文档、用户级与所在课题组。

        Returns:
            (范围说明, 归档记录)，按归档时间从新到旧排列。
        """
        records = [
            (label, record)
            for scope, label in self._editable_scopes()
            for record in self.store.read_archive(scope)
        ]
        records.sort(key=lambda entry: entry[1].archived_at, reverse=True)
        return records

    def restore(self, item_id: str) -> MemoryItem:
        """把一条归档记忆恢复为生效，视为刚被支持；原状态与恢复人记入历史。

        Args:
            item_id: 条目 ID。

        Returns:
            恢复后的条目。

        Raises:
            ValueError: 本人可恢复的归档中没有该条目，或该条目已在使用中。
        """
        for scope, _ in self._editable_scopes():
            if any(
                record.item.id == item_id for record in self.store.read_archive(scope)
            ):
                return self.store.restore(
                    scope,
                    item_id,
                    lambda item: item.revive(
                        f"{self.user_id} 从归档恢复",
                        history_limit=self.settings.history_max_entries,
                    ),
                )
        raise ValueError(f"可恢复的归档中没有该记忆: {item_id}")

    def stats(self) -> list[MemoryStats]:
        """统计本人可见的各级记忆：主文件各状态条目数、归档数与文件大小。

        Returns:
            文档、用户、课题组、通用四行统计。
        """
        documents = self.store.document_scopes(self.tenant_id, self.user_id)
        groups = [
            (f"文档（{len(documents)} 篇）", documents),
            ("用户", [MemoryScope("user", self.tenant_id, self.user_id)]),
            ("课题组", [MemoryScope("tenant", self.tenant_id)]),
            ("通用", [MemoryScope("global")]),
        ]
        rows: list[MemoryStats] = []
        for label, scopes in groups:
            counts: dict[str, int] = {}
            archived = size = 0
            for scope in scopes:
                for item in self.store.load(scope):
                    counts[item.status] = counts.get(item.status, 0) + 1
                archived += len(self.store.read_archive(scope))
                for path in (self.store.path(scope), self.store.archive_path(scope)):
                    size += path.stat().st_size if path.is_file() else 0
            rows.append(MemoryStats(label, counts, archived, size))
        return rows

    def signal_size_bytes(self) -> int:
        """返回本人修改信号文件的字节数。

        Returns:
            字节数；文件不存在时为 0。
        """
        path = self.signals.path
        return path.stat().st_size if path.is_file() else 0

    def _editable_scopes(self) -> list[tuple[MemoryScope, str]]:
        """列出本人可修改、删除与恢复的归属。

        Returns:
            (归属, 范围说明)：本人全部文档、用户级与所在课题组。
        """
        scopes = [
            (scope, f"文档《{self.lineage.paper_title(scope.paper_id)}》")
            for scope in self.store.document_scopes(self.tenant_id, self.user_id)
        ]
        scopes.append((MemoryScope("user", self.tenant_id, self.user_id), "用户"))
        scopes.append((MemoryScope("tenant", self.tenant_id), "课题组"))
        return scopes

    def _change(
        self,
        item_id: str,
        change: Callable[[MemoryItem], None],
    ) -> MemoryItem:
        """在可编辑的级别中修改一条可见记忆。

        Args:
            item_id: 条目 ID。
            change: 原地修改条目的函数。

        Returns:
            修改后的条目。

        Raises:
            ValueError: 条目不存在、不可见或属于通用级。
        """
        entry = self._find(item_id)
        if entry.scope.level not in EDITABLE_LEVELS:
            raise ValueError("通用级记忆由管理员审核维护，不能在此修改或删除")
        changed: list[MemoryItem] = []

        def apply(items: list[MemoryItem]) -> None:
            for item in items:
                if item.id == item_id and item.status in LIVE_STATUSES:
                    change(item)
                    changed.append(item)

        self.store.update(entry.scope, apply)
        if not changed:
            raise ValueError(f"记忆已被其他操作修改，请重新查看: {item_id}")
        return changed[0]

    def _find(self, item_id: str) -> VisibleMemory:
        """在可见记忆中查找条目。

        Args:
            item_id: 条目 ID。

        Returns:
            可见记忆。

        Raises:
            ValueError: 条目不存在或不可见。
        """
        for entry in self.visible():
            if entry.item.id == item_id:
                return entry
        raise ValueError(f"记忆不存在或不可见: {item_id}")

    def _live(
        self,
        scope: MemoryScope,
        label: Callable[[MemoryItem], str],
    ) -> list[VisibleMemory]:
        """读取归属下的生效与休眠条目。

        Args:
            scope: 记忆归属。
            label: 由条目生成范围说明的函数。

        Returns:
            可见记忆列表。
        """
        return [
            VisibleMemory(scope, label(item), item)
            for item in self.store.load(scope)
            if item.status in LIVE_STATUSES
        ]
