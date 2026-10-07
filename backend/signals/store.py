"""修改信号存储：每个用户一个只追加的 JSONL 文件，另有只追加的 ID 索引文件用于去重。

    signals.jsonl   每行一条信号
    signals.ids     每行一个已写入的信号 ID；去重只读这个小文件，不必读全部信号
消费方按字节位置读取新增信号，每轮只读上次位置之后的部分。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from pathlib import Path

from backend.signals.models import Signal

SIGNALS_FILENAME = "signals.jsonl"  # 信号目录下的信号文件名。
IDS_FILENAME = "signals.ids"  # 信号目录下的 ID 索引文件名。


class SignalStore:
    """追加写入修改信号；同一信号重复采集时只保留第一次。"""

    def __init__(self, directory: Path) -> None:
        """初始化信号存储，目录在首次写入时创建。

        Args:
            directory: 当前用户的信号目录。

        Returns:
            None。
        """
        self.path = directory / SIGNALS_FILENAME  # 信号 JSONL 文件路径。
        self.ids_path = directory / IDS_FILENAME  # 信号 ID 索引文件路径。
        self._ids: set[str] | None = None  # 已写入的信号 ID，首次写入前从索引加载。
        self._lock = threading.Lock()  # Hook 与工具线程可能同时写入。

    def append(self, signals: Iterable[Signal]) -> list[Signal]:
        """追加尚未记录过的信号，并同步写入 ID 索引。

        Args:
            signals: 待写入的信号。

        Returns:
            本次实际写入的信号（已去重）。

        Raises:
            OSError: 文件读写失败。
        """
        with self._lock:
            known = self._load_ids()
            fresh: list[Signal] = []
            for signal in signals:
                if signal.id not in known:
                    known.add(signal.id)
                    fresh.append(signal)
            if fresh:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as file:
                    for signal in fresh:
                        file.write(json.dumps(signal.to_dict(), ensure_ascii=False))
                        file.write("\n")
                with self.ids_path.open("a", encoding="utf-8", newline="\n") as file:
                    file.writelines(f"{signal.id}\n" for signal in fresh)
            return fresh

    def read_from(self, position: int) -> list[tuple[Signal, int]]:
        """从字节位置起读取新增信号，只读取完整的行。

        Args:
            position: 上次读到的字节位置。

        Returns:
            (信号, 该行结束后的字节位置) 列表；文件不存在时为空。

        Raises:
            OSError: 文件读取失败。
        """
        with self._lock:
            if not self.path.is_file():
                return []
            with self.path.open("rb") as file:
                file.seek(position)
                data = file.read()
        entries: list[tuple[Signal, int]] = []
        cursor = position
        for raw in data.split(b"\n")[:-1]:  # 最后一段是未写完的行或空串。
            cursor += len(raw) + 1
            if raw.strip():
                entries.append((Signal(**json.loads(raw)), cursor))
        return entries

    def find(self, signal_ids: set[str]) -> list[Signal]:
        """按 ID 查找信号，供查看记忆依据（需要读取全部信号，只在人工查看时使用）。

        Args:
            signal_ids: 信号 ID。

        Returns:
            按写入顺序排列的匹配信号；不在本用户信号中的 ID 被忽略。

        Raises:
            OSError: 文件读取失败。
        """
        return [signal for signal, _ in self.read_from(0) if signal.id in signal_ids]

    def _load_ids(self) -> set[str]:
        """读取已写入的信号 ID，调用方须已持有锁；索引缺失时由信号文件重建一次。

        Returns:
            已写入信号 ID 的集合（缓存，后续写入同步追加）。

        Raises:
            OSError: 文件读写失败。
        """
        if self._ids is None:
            if self.ids_path.is_file():
                self._ids = set(self.ids_path.read_text(encoding="utf-8").split())
            elif self.path.is_file():
                with self.path.open(encoding="utf-8") as file:
                    self._ids = {
                        json.loads(line)["id"] for line in file if line.strip()
                    }
                self.ids_path.write_text(
                    "".join(f"{signal_id}\n" for signal_id in self._ids),
                    encoding="utf-8",
                    newline="\n",
                )
            else:
                self._ids = set()
        return self._ids
