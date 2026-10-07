"""对话轮次：收集一轮内的用户输入、最终回复与工具调用，轮次结束时交给处理函数；对话原文按会话追加写入 JSONL。

原文文件每行一轮：run_id、user_input、reply（失败为空）、error（成功为空）、
tool_calls（name、arguments、error；不含工具结果）、finished_at。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from backend.hooks import HookContext, HookEngine, HookEvent


@dataclass(frozen=True)
class TurnRecord:
    """一轮对话的记录。"""

    run_id: str  # Runtime.run 的唯一标识。
    session_id: str  # 所属会话 ID；未绑定会话时为空。
    user_input: str  # 用户输入。
    reply: str  # 最终回复；失败的轮次为空。
    error: str  # 失败原因；成功的轮次为空。
    tool_calls: list[dict[str, Any]]  # 工具调用：name、arguments、error（不含结果）。
    finished_at: str  # 结束时间，ISO 8601。


class TurnCollector:
    """收集一轮内的工具调用，轮次结束时把完整的轮次记录交给处理函数。"""

    tool_events: ClassVar[tuple[HookEvent, ...]] = (  # 记录工具调用的事件。
        HookEvent.AFTER_TOOL_EXECUTE,
        HookEvent.TOOL_ERROR,
        HookEvent.TOOL_CALL_ERROR,
    )
    run_end_events: ClassVar[tuple[HookEvent, ...]] = (  # 轮次结束事件。
        HookEvent.AFTER_RUNTIME,
        HookEvent.RUNTIME_ERROR,
    )

    def __init__(self, handler: Callable[[TurnRecord], None]) -> None:
        """初始化轮次收集。

        Args:
            handler: 轮次结束时接收轮次记录的函数。

        Returns:
            None。
        """
        self.handler = handler  # 轮次记录的处理函数。
        self._tool_calls: dict[str, list[dict[str, Any]]] = {}  # 各轮的工具调用。

    def register(self, hook_engine: HookEngine) -> None:
        """注册到工具与轮次结束事件，失败只记日志。

        Args:
            hook_engine: Runtime 使用的 HookEngine。

        Returns:
            None。
        """
        for event in (*self.tool_events, *self.run_end_events):
            hook_engine.register(event, self, critical=False)

    def __call__(self, context: HookContext) -> None:
        """记录工具调用，或在轮次结束时交出本轮记录。

        Args:
            context: 生命周期上下文。

        Returns:
            None。
        """
        run_id = context.scope.run_id or ""
        if context.event in self.tool_events:
            self._tool_calls.setdefault(run_id, []).append(
                {
                    "name": context.payload.get("name"),
                    "arguments": context.payload.get("arguments"),
                    "error": _describe(context.error),
                }
            )
            return
        user_input = context.payload.get("user_input")
        self.handler(
            TurnRecord(
                run_id=run_id,
                session_id=context.scope.session_id or "",
                user_input=user_input if isinstance(user_input, str) else "",
                reply=context.result if isinstance(context.result, str) else "",
                error=_describe(context.error),
                tool_calls=self._tool_calls.pop(run_id, []),
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        )


class TranscriptWriter:
    """把轮次记录按会话追加写入 <session_id>.jsonl，弥补上下文压缩丢弃的早期原文。"""

    def __init__(self, directory: Path) -> None:
        """初始化原文记录，目录在首次写入时创建。

        Args:
            directory: 当前用户的对话原文目录。

        Returns:
            None。
        """
        self.directory = directory  # 对话原文目录。

    def __call__(self, record: TurnRecord) -> None:
        """追加写入一轮记录；未绑定会话的轮次不写。

        Args:
            record: 轮次记录。

        Returns:
            None。

        Raises:
            OSError: 文件写入失败。
        """
        if not record.session_id:
            return
        data = asdict(record)
        del data["session_id"]
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{record.session_id}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(data, ensure_ascii=False, default=str))
            file.write("\n")


def _describe(error: BaseException | None) -> str:
    """把异常描述为单行文本。

    Args:
        error: 异常；没有时为 None。

    Returns:
        ``类型: 信息``；没有异常时为空字符串。
    """
    if error is None:
        return ""
    return f"{type(error).__name__}: {error}"
