"""跨进程文件锁：以独占方式创建锁文件，适用于 Windows 与 POSIX；供多名学生同时写课题组、通用数据时使用。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

_POLL_SECONDS = 0.05  # 等待锁时的轮询间隔。


class FileLock:
    """在目标文件旁创建 ``<文件名>.lock`` 作为锁；持有者异常退出留下的锁超过时限后自动清除。"""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float,
        stale_seconds: float,
    ) -> None:
        """初始化文件锁。

        Args:
            path: 被保护的文件路径。
            timeout_seconds: 等待锁的最长秒数。
            stale_seconds: 锁文件超过该秒数未释放时视为残留。

        Returns:
            None。
        """
        self.lock_path = path.with_name(f"{path.name}.lock")  # 锁文件路径。
        self.timeout_seconds = timeout_seconds  # 等待锁的最长秒数。
        self.stale_seconds = stale_seconds  # 残留锁的判定秒数。

    def __enter__(self) -> FileLock:
        """获取锁，必要时创建所在目录。

        Returns:
            自身。

        Raises:
            TimeoutError: 超时仍未获得锁。
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                self._clear_stale()
                if time.monotonic() > deadline:
                    raise TimeoutError(f"等待文件锁超时: {self.lock_path}") from None
                time.sleep(_POLL_SECONDS)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(str(os.getpid()))
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """释放锁。

        Args:
            exc_type: 异常类型。
            exc: 异常。
            traceback: 调用栈。

        Returns:
            None。
        """
        self.lock_path.unlink(missing_ok=True)

    def _clear_stale(self) -> None:
        """清除超过时限仍未释放的残留锁文件。

        Returns:
            None。
        """
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return
        if age > self.stale_seconds:
            self.lock_path.unlink(missing_ok=True)
