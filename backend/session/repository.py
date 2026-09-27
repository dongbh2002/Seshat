"""会话文件仓库：一个会话一个 JSON 文件，按会话 ID 读写；只负责存取，不解释会话状态内容。"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # 会话文件格式版本，结构不兼容变化时递增，旧版本文件拒绝加载。
_SESSION_ID_PATTERN = re.compile(  # 会话 ID 格式，同时防止外部输入的 ID 造成路径穿越。
    r"^\d{8}-\d{6}-[0-9a-f]{6}$"
)


@dataclass
class SessionRecord:
    """一个会话的元信息与完整会话状态，对应一个会话文件。"""

    id: str  # 会话 ID：创建时间 YYYYMMDD-HHMMSS 加 6 位随机十六进制。
    title: str  # 会话标题，取首条用户输入压成的单行；尚无输入时为空字符串。
    created_at: str  # 创建时间，带时区的 ISO 8601。
    updated_at: str  # 最后保存时间，带时区的 ISO 8601。
    turn_count: int  # 成功完成的对话轮数。
    state: dict[str, Any]  # Runtime.export_state 导出的会话状态；尚未保存时为空。


class SessionRepository:
    """在单一目录下读写会话文件，写入先落临时文件再原子替换。"""

    def __init__(self, directory: Path) -> None:
        """初始化会话仓库；目录在首次保存时创建。

        Args:
            directory: 会话文件所在目录，通常为 <sessions.root>/<tenant>/<user>。

        Returns:
            None。
        """
        self.directory = directory  # 会话文件所在目录。

    def create(self) -> SessionRecord:
        """创建一个尚未落盘的新会话记录。

        Returns:
            标题为空、轮数为 0、状态为空的会话记录。
        """
        now = datetime.now().astimezone()
        timestamp = now.isoformat(timespec="seconds")
        return SessionRecord(
            id=f"{now:%Y%m%d-%H%M%S}-{uuid4().hex[:6]}",
            title="",
            created_at=timestamp,
            updated_at=timestamp,
            turn_count=0,
            state={},
        )

    def save(self, record: SessionRecord) -> None:
        """把会话记录写入文件，并把 updated_at 更新为当前时间。

        Args:
            record: 需要保存的会话记录，会被原地更新 updated_at。

        Returns:
            None。

        Raises:
            ValueError: 会话 ID 格式无效。
            OSError: 目录创建或文件写入失败。
        """
        path = self._path(record.id)
        record.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        content = json.dumps(
            {"schema_version": SCHEMA_VERSION, **asdict(record)},
            ensure_ascii=False,
            indent=2,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.write_text(content, encoding="utf-8")
        os.replace(temporary_path, path)

    def load(self, session_id: str) -> SessionRecord:
        """读取指定会话。

        Args:
            session_id: 会话 ID。

        Returns:
            文件中保存的会话记录。

        Raises:
            ValueError: 会话 ID 格式无效，或文件内容、版本不兼容。
            FileNotFoundError: 会话文件不存在。
        """
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        return self._read(path)

    def list_records(self) -> list[SessionRecord]:
        """列出全部可读取的会话，无法读取的文件记录警告后跳过。

        Returns:
            按 updated_at 从新到旧排序的会话记录。
        """
        if not self.directory.is_dir():
            return []
        records: list[SessionRecord] = []
        for path in self.directory.glob("*.json"):
            try:
                records.append(self._read(path))
            except (OSError, ValueError) as error:
                _LOGGER.warning("跳过无法读取的会话文件 %s: %s", path, error)
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    # TODO: 删除、重命名会话。

    def _path(self, session_id: str) -> Path:
        """校验会话 ID 并返回对应文件路径。

        Args:
            session_id: 会话 ID。

        Returns:
            会话文件路径。

        Raises:
            ValueError: 会话 ID 格式无效。
        """
        if not _SESSION_ID_PATTERN.match(session_id):
            raise ValueError(f"会话 ID 格式无效: {session_id}")
        return self.directory / f"{session_id}.json"

    @staticmethod
    def _read(path: Path) -> SessionRecord:
        """解析会话文件。

        Args:
            path: 会话文件路径。

        Returns:
            会话记录。

        Raises:
            OSError: 文件读取失败。
            ValueError: 内容不是有效 JSON、版本不兼容或字段不匹配。
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"会话文件根节点必须是对象: {path.name}")
        version = data.pop("schema_version", None)
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"会话文件版本不兼容: {path.name}（{version}，当前 {SCHEMA_VERSION}）"
            )
        try:
            return SessionRecord(**data)
        except TypeError as error:
            raise ValueError(f"会话文件字段无效: {path.name}（{error}）") from error
