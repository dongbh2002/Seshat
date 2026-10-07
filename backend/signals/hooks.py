"""信号采集 Hook：工具见到文档版本时登记版本；审阅状态中问题的结局与用户决定转为修改信号。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from backend.hooks import HookContext, HookEngine, HookEvent
from backend.session import ReviewStateStore
from backend.signals.collector import FeedbackCollector
from backend.signals.models import Signal
from backend.signals.store import SignalStore
from backend.utils.docx import DocumentIndex


class VersionObserverHook:
    """工具结果含 path 与 revision 即视为见到该文档版本，交给 FeedbackCollector 登记。

    结果另含 previous_revision 时（write_document），视为由该版本修改得到。
    """

    def __init__(self, collector: FeedbackCollector) -> None:
        """初始化版本观察 Hook。

        Args:
            collector: 信号采集编排。

        Returns:
            None。
        """
        self.collector = collector  # 信号采集编排。

    def register(self, hook_engine: HookEngine) -> None:
        """注册到工具成功执行事件，失败只记日志不影响工具结果。

        Args:
            hook_engine: ToolEngine 使用的 HookEngine。

        Returns:
            None。
        """
        hook_engine.register(HookEvent.AFTER_TOOL_EXECUTE, self, critical=False)

    def __call__(self, context: HookContext) -> None:
        """登记工具结果中的文档版本。

        Args:
            context: 工具执行后的生命周期上下文。

        Returns:
            None。
        """
        result = context.result
        if not isinstance(result, Mapping):
            return
        path = result.get("path")
        revision = result.get("revision")
        if not isinstance(path, str) or not isinstance(revision, str):
            return
        previous = result.get("previous_revision")
        self.collector.observe(
            path,
            revision,
            previous_revision=previous if isinstance(previous, str) else None,
            session_id=context.scope.session_id or "",
        )


class ReviewFeedbackHook:
    """把 update_review_state 中问题状态的变化与用户决定转为修改信号。"""

    tool_name: ClassVar[str] = "update_review_state"  # 监听的工具名称。
    recorded_statuses: ClassVar[frozenset[str]] = frozenset(  # 记为信号的问题状态。
        {"accepted", "rejected", "resolved"}
    )
    user_role: ClassVar[str] = "student"  # 对话中的用户在课题组中的角色。

    def __init__(
        self,
        review_state: ReviewStateStore,
        document_index: DocumentIndex,
        store: SignalStore,
    ) -> None:
        """初始化审阅反馈 Hook。

        Args:
            review_state: 当前会话的审阅状态。
            document_index: 用于查询问题所在块的当前正文与标题路径。
            store: 当前用户的信号存储。

        Returns:
            None。
        """
        self.review_state = review_state  # 当前会话的审阅状态。
        self.document_index = document_index  # 文档结构索引。
        self.store = store  # 信号存储。

    def register(self, hook_engine: HookEngine) -> None:
        """注册到工具成功执行事件，失败只记日志不影响工具结果。

        Args:
            hook_engine: ToolEngine 使用的 HookEngine。

        Returns:
            None。
        """
        hook_engine.register(HookEvent.AFTER_TOOL_EXECUTE, self, critical=False)

    def __call__(self, context: HookContext) -> None:
        """把已生效的 set_status 与 add_decision 操作写为信号。

        工具中途失败时只触发 TOOL_ERROR，此前已生效的操作不会被采集。

        Args:
            context: 工具执行后的生命周期上下文。

        Returns:
            None。
        """
        if context.payload.get("name") != self.tool_name:
            return
        arguments = context.payload.get("arguments")
        result = context.result
        if not isinstance(arguments, Mapping) or not isinstance(result, Mapping):
            return
        operations = arguments.get("operations")
        results = result.get("results")
        if not isinstance(operations, list) or not isinstance(results, list):
            return

        actor = context.scope.user_id or ""
        session_id = context.scope.session_id or ""
        signals: list[Signal] = []
        for operation, outcome in zip(operations, results):
            op = outcome.get("op")
            if op == "set_status":
                status = str(operation.get("status", "")).strip()
                if status in self.recorded_statuses:
                    signals.append(
                        self._finding_signal(
                            outcome["finding_id"], status, actor, session_id
                        )
                    )
            elif op == "add_decision":
                signals.append(
                    Signal.create(
                        source="decision",
                        actor=actor,
                        role=self.user_role,
                        document="",
                        revision="",
                        session_id=session_id,
                        comment=str(operation.get("text", "")).strip(),
                    )
                )
        self.store.append(signals)

    def _finding_signal(
        self,
        finding_id: str,
        status: str,
        actor: str,
        session_id: str,
    ) -> Signal:
        """把一条问题的状态变化转为信号。

        Args:
            finding_id: 问题编号。
            status: 新状态。
            actor: 当前用户 ID。
            session_id: 当前会话 ID。

        Returns:
            finding_outcome 信号；resolved 时 after 为问题所在块的当前正文。
        """
        finding = self.review_state.get_finding(finding_id)
        revision = section = after = ""
        try:
            snapshot = self.document_index.load(finding.path)
        except (OSError, ValueError):
            snapshot = None
        if snapshot is not None:
            revision = snapshot.revision
            if finding.block_id in snapshot.block_ids:
                section = snapshot.get_heading_path(finding.block_id)
                if status == "resolved":
                    after = snapshot.get_block_text(finding.block_id)
        return Signal.create(
            source="finding_outcome",
            actor=actor,
            role=self.user_role,
            document=finding.path,
            revision=revision,
            session_id=session_id,
            block_id=finding.block_id,
            section=section,
            before=finding.excerpt,
            after=after,
            comment=finding.issue,
            outcome=status,
        )
