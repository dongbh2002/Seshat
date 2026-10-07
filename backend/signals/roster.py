"""课题组成员表：Word 修订与批注的作者名到角色的映射，按租户保存，组内学生共享。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.signals.models import AUTHOR_ROLES
from backend.utils.file_lock import FileLock

ROSTER_FILENAME = "roster.json"  # 课题组目录下的成员表文件名。
_SCHEMA_VERSION = 1  # 成员表文件格式版本。


class AuthorRoster:
    """读写课题组成员表；每次读取都从文件加载，以看到组内其他学生登记的作者。"""

    def __init__(
        self,
        directory: Path,
        *,
        lock_timeout_seconds: float,
        lock_stale_seconds: float,
    ) -> None:
        """初始化成员表，目录在首次写入时创建。

        Args:
            directory: 当前课题组的共享数据目录。
            lock_timeout_seconds: 等待文件锁的最长秒数。
            lock_stale_seconds: 残留锁文件的判定秒数。

        Returns:
            None。
        """
        self.path = directory / ROSTER_FILENAME  # 成员表文件路径。
        self.lock_timeout_seconds = lock_timeout_seconds  # 等待文件锁的最长秒数。
        self.lock_stale_seconds = lock_stale_seconds  # 残留锁文件的判定秒数。

    def get_roles(self) -> dict[str, str]:
        """读取全部已登记作者的角色。

        Returns:
            作者名到角色的映射；成员表不存在时为空。

        Raises:
            ValueError: 文件格式版本不符。
        """
        return {
            author: entry["role"] for author, entry in self._read()["authors"].items()
        }

    def update(self, roles: Mapping[str, str], *, updated_by: str) -> None:
        """登记或更新作者角色。

        Args:
            roles: 作者名到角色的映射。
            updated_by: 登记人的用户 ID。

        Returns:
            None。

        Raises:
            ValueError: 作者名为空或角色无效。
            OSError: 文件写入失败。
            TimeoutError: 等待文件锁超时。
        """
        if not roles:
            return
        for author, role in roles.items():
            if not author.strip():
                raise ValueError("作者名不能为空")
            if role not in AUTHOR_ROLES:
                raise ValueError(
                    f"作者角色无效: {role}（可选 {', '.join(AUTHOR_ROLES)}）"
                )
        file_lock = FileLock(
            self.path,
            timeout_seconds=self.lock_timeout_seconds,
            stale_seconds=self.lock_stale_seconds,
        )
        with file_lock:
            data = self._read()
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            for author, role in roles.items():
                data["authors"][author] = {
                    "role": role,
                    "updated_by": updated_by,
                    "updated_at": now,
                }
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, self.path)

    def _read(self) -> dict[str, Any]:
        """读取成员表文件。

        Returns:
            ``schema_version`` 与 ``authors``（作者名到 role/updated_by/updated_at）；
            文件不存在时为空表。

        Raises:
            ValueError: 文件格式版本不符。
        """
        if not self.path.is_file():
            return {"schema_version": _SCHEMA_VERSION, "authors": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(f"成员表格式版本不符: {self.path}")
        return data
