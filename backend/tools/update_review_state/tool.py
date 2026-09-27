"""定义记录长文档审阅进度、问题和用户决定的 Agent 工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from backend.session import FINDING_STATUSES, ReviewStateStore
from backend.tools.base import BaseTool, ToolImpact
from backend.utils.docx import DocumentIndex


class UpdateReviewStateTool(BaseTool):
    """把审阅结论写入 ReviewStateStore，使其在正文被压缩后仍可用。"""

    name: ClassVar[str] = "update_review_state"  # 模型调用审阅状态记录时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的审阅状态记录说明。
        "记录已审阅的内容块、发现的问题、问题状态变化和用户决定。"
        "文档正文会从历史上下文中清理，未记录的结论会丢失。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.WRITE  # 修改会话审阅状态。
    timeout_seconds: ClassVar[float] = 10.0  # 内存状态更新的超时时间。
    parameters: ClassVar[dict[str, Any]] = {  # 审阅状态工具的输入参数定义。
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "minItems": 1,
                "description": "按顺序执行的审阅状态操作。",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "mark_reviewed",
                                "add_finding",
                                "set_status",
                                "add_decision",
                            ],
                            "description": "操作类型。",
                        },
                        "path": {
                            "type": "string",
                            "description": "mark_reviewed/add_finding 的文档路径。",
                        },
                        "start_id": {
                            "type": "string",
                            "description": "mark_reviewed 已审阅范围的首个块 ID。",
                        },
                        "end_id": {
                            "type": "string",
                            "description": "mark_reviewed 已审阅范围的末尾块 ID（含）。",
                        },
                        "block_id": {
                            "type": "string",
                            "description": "add_finding 的问题所在内容块 ID。",
                        },
                        "issue": {
                            "type": "string",
                            "description": "add_finding 的问题描述及修改建议。",
                        },
                        "finding_id": {
                            "type": "string",
                            "description": "set_status 的问题编号。",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(FINDING_STATUSES),
                            "description": "set_status 的新状态。",
                        },
                        "text": {
                            "type": "string",
                            "description": "add_decision 的用户决定或偏好。",
                        },
                    },
                    "required": ["op"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["operations"],
        "additionalProperties": False,
    }

    def __init__(self, store: ReviewStateStore, document_index: DocumentIndex) -> None:
        """初始化审阅状态工具。

        Args:
            store: 与上下文引擎共享的审阅状态存储。
            document_index: 用于解析块范围和校验块 ID 的文档结构索引。

        Returns:
            None。
        """
        self.store = store  # 工具写入的审阅状态存储。
        self.document_index = document_index  # 解析块范围和校验块 ID 的索引。

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """按顺序执行审阅状态操作。

        Args:
            **arguments: 包含 operations 列表的调用参数。

        Returns:
            ``ok`` 与每个操作结果组成的字典；add_finding 结果含新问题编号。

        Raises:
            ValueError: 参数缺失或无效；错误信息说明已生效的操作数。
        """
        operations = arguments.get("operations")
        if not isinstance(operations, list) or not operations:
            raise ValueError("operations 必须是非空数组")

        results: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            try:
                results.append(self._apply(operation))
            except (OSError, TypeError, ValueError) as error:
                raise ValueError(
                    f"第 {index + 1} 个操作失败（前 {index} 个已生效）: {error}"
                ) from error
        return {"ok": True, "results": results}

    def _apply(self, operation: Any) -> dict[str, Any]:
        """执行单个审阅状态操作。

        Args:
            operation: 模型生成的单个操作对象。

        Returns:
            该操作的结果描述。

        Raises:
            TypeError: 操作不是对象。
            ValueError: 操作类型未知、字段无效或引用不存在。
        """
        if not isinstance(operation, Mapping):
            raise TypeError("操作必须是对象")
        op = operation.get("op")
        if op == "mark_reviewed":
            snapshot = self.document_index.load(self._require_text(operation, "path"))
            block_ids = snapshot.block_range(
                self._require_text(operation, "start_id"),
                self._require_text(operation, "end_id"),
            )
            self.store.mark_reviewed(snapshot.path, snapshot.get_block_texts(block_ids))
            return {"op": op, "marked_count": len(block_ids)}
        if op == "add_finding":
            snapshot = self.document_index.load(self._require_text(operation, "path"))
            block_id = self._require_text(operation, "block_id")
            if block_id not in snapshot.block_ids:
                raise ValueError(f"内容块 ID 不存在: {block_id}")
            finding_id = self.store.add_finding(
                snapshot.path,
                block_id,
                self._require_text(operation, "issue"),
                excerpt=snapshot.get_block_text(block_id),
            )
            return {"op": op, "finding_id": finding_id}
        if op == "set_status":
            finding_id = self._require_text(operation, "finding_id")
            self.store.set_finding_status(
                finding_id,
                self._require_text(operation, "status"),
            )
            return {"op": op, "finding_id": finding_id}
        if op == "add_decision":
            self.store.add_decision(self._require_text(operation, "text"))
            return {"op": op}
        raise ValueError(f"未知操作类型: {op}")

    @staticmethod
    def _require_text(operation: Mapping[str, Any], key: str) -> str:
        """读取操作中的必填非空字符串字段。

        Args:
            operation: 单个操作对象。
            key: 字段名。

        Returns:
            去除首尾空白后的字段值。

        Raises:
            ValueError: 字段缺失、类型错误或为空。
        """
        value = operation.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} 必须是非空字符串")
        return value.strip()
