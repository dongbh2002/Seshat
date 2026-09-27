"""审阅状态 Hook：读取后登记 revision；原地修改后记录被改动的块并取消其已审阅标记。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from backend.hooks import HookContext, HookEngine, HookEvent
from backend.session.review_state import ReviewStateStore


class ReviewStateHook:
    """监听文档读写工具的结果，使 ReviewStateStore 与文档当前版本保持一致。"""

    read_tool_name: ClassVar[str] = "read_document"  # 读取后登记文档的工具名称。
    write_tool_name: ClassVar[str] = "write_document"  # 修改后同步状态的工具名称。
    content_changing_ops: ClassVar[frozenset[str]] = frozenset(  # 改变块内容的操作。
        {"replace_text", "set_cell", "delete"}
    )
    insert_op: ClassVar[str] = "insert_after"  # 在目标块后插入新块的操作。
    delete_op: ClassVar[str] = "delete"  # 删除目标块的操作。

    def __init__(self, store: ReviewStateStore) -> None:
        """初始化审阅状态 Hook。

        Args:
            store: 需要同步的审阅状态存储。

        Returns:
            None。
        """
        self.store = store  # 被写入的审阅状态存储。

    def register(self, hook_engine: HookEngine) -> None:
        """注册到工具成功执行事件。

        Args:
            hook_engine: ToolEngine 使用的 HookEngine。

        Returns:
            None。
        """
        hook_engine.register(HookEvent.AFTER_TOOL_EXECUTE, self, critical=False)

    def __call__(self, context: HookContext) -> None:
        """按工具类型分发处理。

        Args:
            context: 工具执行后的生命周期上下文。

        Returns:
            None。
        """
        result = context.result
        if not isinstance(result, Mapping):
            return
        name = context.payload.get("name")
        if name == self.read_tool_name:
            self._on_read(result)
        elif name == self.write_tool_name:
            self._on_write(result)

    def _on_read(self, result: Mapping[str, Any]) -> None:
        """登记被读取文档的 path 和 revision。

        Args:
            result: read_document 的返回结果。

        Returns:
            None。
        """
        path = result.get("path")
        revision = result.get("revision")
        if isinstance(path, str) and isinstance(revision, str):
            self.store.track_document(path, revision)

    def _on_write(self, result: Mapping[str, Any]) -> None:
        """原地修改后登记新版本和被改动的块，并取消内容被改动的块的已审阅标记。

        被改动的块用于判断旧读取结果是否过时：内容变化的目标块，以及
        insert_after 的锚点块（旧读取缺少新插入的段落）。插入、直接删除
        或为段落补齐 paraId 都会使位置型块 ID 改变，记为结构变化。
        另存为新文件时源文档未变，不做处理。

        Args:
            result: write_document 的返回结果。

        Returns:
            None。
        """
        path = result.get("path")
        previous_revision = result.get("previous_revision")
        revision = result.get("revision")
        operations = result.get("operations")
        if (
            not isinstance(path, str)
            or path != result.get("source_path")
            or not isinstance(previous_revision, str)
            or not isinstance(revision, str)
            or not isinstance(operations, list)
        ):
            return
        valid_operations = [
            operation
            for operation in operations
            if isinstance(operation, Mapping) and isinstance(operation.get("id"), str)
        ]
        content_changed = [
            operation["id"]
            for operation in valid_operations
            if operation.get("op") in self.content_changing_ops
        ]
        insert_anchors = [
            operation["id"]
            for operation in valid_operations
            if operation.get("op") == self.insert_op
        ]
        structural = (
            bool(insert_anchors)
            or bool(result.get("assigned_para_id_count"))
            or (
                result.get("mode") == "direct"
                and any(
                    operation.get("op") == self.delete_op
                    for operation in valid_operations
                )
            )
        )
        self.store.record_changes(
            path,
            previous_revision,
            revision,
            [*content_changed, *insert_anchors],
            structural=structural,
        )
        self.store.unmark_reviewed(path, content_changed)
