"""记忆存储：每个归属一个主文件（热数据）与一个只追加的归档文件（冷数据），主文件按修改时间缓存。

目录布局：
    <memory_root>/<tenant_id>/<user_id>/user.json                 用户级
    <memory_root>/<tenant_id>/<user_id>/documents/<paper_id>.json  文档级
    <tenants_root>/<tenant_id>/memory.json                         课题组级
    <global_directory>/memory.json                                 通用级
主文件为 {"schema_version": 1, "items": [MemoryItem, ...]}；同目录下 <主文件名>.archive.jsonl 为归档，
每行 {"archived_at": 归档时间, "reason": 归档原因, "item": MemoryItem}，永久保留，可恢复到主文件。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.memory.models import MemoryItem, MemoryScope
from backend.utils.file_lock import FileLock

MEMORY_FILENAME = "memory.json"  # 课题组级与通用级的记忆文件名。
USER_FILENAME = "user.json"  # 用户级记忆文件名。
DOCUMENTS_DIRNAME = "documents"  # 用户目录下存放文档级记忆的子目录名。
ARCHIVE_SUFFIX = ".archive.jsonl"  # 归档文件名后缀，接在主文件名（不含扩展名）之后。
_SCHEMA_VERSION = 1  # 记忆文件格式版本。


@dataclass(frozen=True)
class ArchivedMemory:
    """归档中的一条记忆。"""

    archived_at: str  # 归档时间，ISO 8601。
    reason: str  # 归档原因。
    item: MemoryItem  # 归档时的条目。


class MemoryStore:
    """读写四级记忆文件。"""

    def __init__(
        self,
        *,
        memory_root: Path,
        tenants_root: Path,
        global_directory: Path,
        lock_timeout_seconds: float,
        lock_stale_seconds: float,
    ) -> None:
        """初始化记忆存储，目录在首次写入时创建。

        Args:
            memory_root: 各用户记忆的根目录。
            tenants_root: 各课题组共享数据的根目录。
            global_directory: 通用数据目录。
            lock_timeout_seconds: 等待文件锁的最长秒数。
            lock_stale_seconds: 残留锁文件的判定秒数。

        Returns:
            None。
        """
        self.memory_root = memory_root  # 各用户记忆的根目录。
        self.tenants_root = tenants_root  # 各课题组共享数据的根目录。
        self.global_directory = global_directory  # 通用数据目录。
        self.lock_timeout_seconds = lock_timeout_seconds  # 等待文件锁的最长秒数。
        self.lock_stale_seconds = lock_stale_seconds  # 残留锁文件的判定秒数。
        self._cache: dict[Path, tuple[float, list[MemoryItem]]] = {}  # 文件缓存。
        self._lock = threading.Lock()  # 工具线程与 Hook 可能同时读写。

    def path(self, scope: MemoryScope) -> Path:
        """返回归属对应的记忆文件路径。

        Args:
            scope: 记忆归属。

        Returns:
            记忆文件路径（不保证存在）。

        Raises:
            ValueError: 归属缺少所需的 ID。
        """
        if scope.level == "global":
            return self.global_directory / MEMORY_FILENAME
        if not scope.tenant_id:
            raise ValueError(f"{scope.level} 级记忆缺少课题组 ID")
        if scope.level == "tenant":
            return self.tenants_root / scope.tenant_id / MEMORY_FILENAME
        if not scope.user_id:
            raise ValueError(f"{scope.level} 级记忆缺少用户 ID")
        user_directory = self.memory_root / scope.tenant_id / scope.user_id
        if scope.level == "user":
            return user_directory / USER_FILENAME
        if not scope.paper_id:
            raise ValueError("文档级记忆缺少文档 ID")
        return user_directory / DOCUMENTS_DIRNAME / f"{scope.paper_id}.json"

    def load(self, scope: MemoryScope) -> list[MemoryItem]:
        """读取归属下的全部条目。

        Args:
            scope: 记忆归属。

        Returns:
            条目列表（独立副本）；文件不存在时为空。

        Raises:
            ValueError: 文件格式版本不符。
        """
        with self._lock:
            return [
                MemoryItem(**item.to_dict()) for item in self._read(self.path(scope))
            ]

    def update(
        self,
        scope: MemoryScope,
        change: Callable[[list[MemoryItem]], None],
    ) -> list[MemoryItem]:
        """在进程内锁与跨进程文件锁保护下读取、修改并原子写回归属下的条目。

        课题组级与通用级会被多名学生的进程同时修改，读取总以磁盘为准。

        Args:
            scope: 记忆归属。
            change: 原地修改条目列表的函数。

        Returns:
            修改后的条目列表。

        Raises:
            ValueError: 文件格式版本不符。
            OSError: 文件写入失败。
            TimeoutError: 等待文件锁超时。
        """
        path = self.path(scope)
        with self._lock, self._file_lock(path):
            items = [MemoryItem(**item.to_dict()) for item in self._read(path)]
            change(items)
            self._write(path, items)
            return [MemoryItem(**item.to_dict()) for item in items]

    def archive_path(self, scope: MemoryScope) -> Path:
        """返回归属的归档文件路径。

        Args:
            scope: 记忆归属。

        Returns:
            与主文件同目录的 <主文件名>.archive.jsonl（不保证存在）。
        """
        path = self.path(scope)
        return path.with_name(f"{path.stem}{ARCHIVE_SUFFIX}")

    def archive(
        self,
        scope: MemoryScope,
        choose: Callable[[list[MemoryItem]], Mapping[str, str]],
    ) -> list[MemoryItem]:
        """把选中的条目从主文件移入归档。

        先追加归档再写回主文件：中途失败时条目可能同时出现在两处，但不会丢失。

        Args:
            scope: 记忆归属。
            choose: 由主文件全部条目选出要归档的条目，返回条目 ID 到归档原因。

        Returns:
            移入归档的条目。

        Raises:
            OSError: 文件写入失败。
            TimeoutError: 等待文件锁超时。
        """
        path = self.path(scope)
        with self._lock, self._file_lock(path):
            items = [MemoryItem(**item.to_dict()) for item in self._read(path)]
            reasons = choose(items)
            moved = [item for item in items if item.id in reasons]
            if not moved:
                return []
            archive = self.archive_path(scope)
            archive.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with archive.open("a", encoding="utf-8", newline="\n") as file:
                for item in moved:
                    record = {
                        "archived_at": now,
                        "reason": reasons[item.id],
                        "item": item.to_dict(),
                    }
                    file.write(json.dumps(record, ensure_ascii=False))
                    file.write("\n")
            self._write(path, [item for item in items if item.id not in reasons])
            return moved

    def read_archive(self, scope: MemoryScope) -> list[ArchivedMemory]:
        """读取归属的全部归档记录。

        Args:
            scope: 记忆归属。

        Returns:
            按归档顺序排列的记录；没有归档时为空。

        Raises:
            OSError: 文件读取失败。
        """
        archive = self.archive_path(scope)
        if not archive.is_file():
            return []
        records: list[ArchivedMemory] = []
        with archive.open(encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    data = json.loads(line)
                    records.append(
                        ArchivedMemory(
                            archived_at=data["archived_at"],
                            reason=data["reason"],
                            item=MemoryItem(**data["item"]),
                        )
                    )
        return records

    def restore(
        self,
        scope: MemoryScope,
        item_id: str,
        revive: Callable[[MemoryItem], None],
    ) -> MemoryItem:
        """把归档中的条目（取最后一次归档的版本）恢复到主文件；归档记录保留。

        Args:
            scope: 记忆归属。
            item_id: 条目 ID。
            revive: 恢复前原地调整条目（状态、时间、历史）的函数。

        Returns:
            恢复后的条目。

        Raises:
            ValueError: 归档中没有该条目，或主文件中已有该条目。
            OSError: 文件读写失败。
            TimeoutError: 等待文件锁超时。
        """
        path = self.path(scope)
        with self._lock, self._file_lock(path):
            matches = [
                record
                for record in self.read_archive(scope)
                if record.item.id == item_id
            ]
            if not matches:
                raise ValueError(f"归档中没有该记忆: {item_id}")
            items = [MemoryItem(**item.to_dict()) for item in self._read(path)]
            if any(item.id == item_id for item in items):
                raise ValueError(f"该记忆已在使用中，无需恢复: {item_id}")
            item = MemoryItem(**matches[-1].item.to_dict())
            revive(item)
            items.append(item)
            self._write(path, items)
            return MemoryItem(**item.to_dict())

    def tenant_ids(self) -> list[str]:
        """列出有用户记忆或课题组记忆的全部课题组。

        Returns:
            按名称排序的课题组 ID。
        """
        ids = {path.name for path in self.memory_root.glob("*") if path.is_dir()}
        ids |= {
            path.parent.name for path in self.tenants_root.glob(f"*/{MEMORY_FILENAME}")
        }
        return sorted(ids)

    def user_ids(self, tenant_id: str) -> list[str]:
        """列出课题组内有记忆目录的全部用户。

        Args:
            tenant_id: 课题组 ID。

        Returns:
            按名称排序的用户 ID。
        """
        directory = self.memory_root / tenant_id
        return sorted(path.name for path in directory.glob("*") if path.is_dir())

    def document_scopes(self, tenant_id: str, user_id: str) -> list[MemoryScope]:
        """列出用户已有的文档级记忆归属。

        Args:
            tenant_id: 课题组 ID。
            user_id: 用户 ID。

        Returns:
            文档级归属列表。
        """
        directory = self.memory_root / tenant_id / user_id / DOCUMENTS_DIRNAME
        return [
            MemoryScope("document", tenant_id, user_id, path.stem)
            for path in sorted(directory.glob("*.json"))
        ]

    def user_scopes(self, tenant_id: str) -> list[MemoryScope]:
        """列出课题组内已有用户级记忆的归属。

        Args:
            tenant_id: 课题组 ID。

        Returns:
            用户级归属列表。
        """
        directory = self.memory_root / tenant_id
        return [
            MemoryScope("user", tenant_id, path.parent.name)
            for path in sorted(directory.glob(f"*/{USER_FILENAME}"))
        ]

    def tenant_scopes(self) -> list[MemoryScope]:
        """列出已有课题组级记忆的归属。

        Returns:
            课题组级归属列表。
        """
        return [
            MemoryScope("tenant", path.parent.name)
            for path in sorted(self.tenants_root.glob(f"*/{MEMORY_FILENAME}"))
        ]

    def _file_lock(self, path: Path) -> FileLock:
        """创建保护主文件与其归档的跨进程文件锁。

        Args:
            path: 主文件路径。

        Returns:
            未获取的文件锁。
        """
        return FileLock(
            path,
            timeout_seconds=self.lock_timeout_seconds,
            stale_seconds=self.lock_stale_seconds,
        )

    def _write(self, path: Path, items: list[MemoryItem]) -> None:
        """原子写入主文件并更新缓存；调用方须已持有锁。

        Args:
            path: 主文件路径。
            items: 全部条目。

        Returns:
            None。

        Raises:
            OSError: 文件写入失败。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "items": [item.to_dict() for item in items],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
        self._cache[path] = (path.stat().st_mtime, items)

    def _read(self, path: Path) -> list[MemoryItem]:
        """读取记忆文件，文件未变化时返回缓存；调用方须已持有锁。

        Args:
            path: 记忆文件路径。

        Returns:
            缓存中的条目列表（调用方不得修改）。

        Raises:
            ValueError: 文件格式版本不符。
        """
        if not path.is_file():
            return []
        mtime = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(f"记忆文件格式版本不符: {path}")
        items = [MemoryItem(**item) for item in data["items"]]
        self._cache[path] = (mtime, items)
        return items
