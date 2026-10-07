"""记忆 agent 的触发：每轮结束后在后台调用记忆 agent；会话结束时等待其完成，再晋升、维护与生成通用候选。

会话结束时的顺序：
    等待后台任务 → 处理遗留信号 → 用户级有变化时晋升课题组共性
    → 用户级、课题组级标记休眠，有变化的再由模型整理本会话变动的条目 → 课题组级有变化时生成通用候选
    → 用户级、课题组级与本会话涉及文档的文档级归档治理
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any

from backend.hooks import HookContext, HookEngine, HookEvent
from backend.memory.maintenance import MemoryMaintenance
from backend.memory.models import MemoryScope
from backend.memory.promotion import MemoryPromotion
from backend.memory.recorder import MemoryRecorder
from backend.memory.retriever import MemoryRetriever
from backend.signals import TurnCollector, TurnRecord

_LOGGER = logging.getLogger(__name__)


class MemoryAgentHook:
    """轮次结束时把本轮交给后台的记忆 agent，不阻塞回复；会话结束时收尾、晋升并维护。

    记忆 agent 只在一个后台线程中串行运行；每一步失败只记日志，不影响对话与后续步骤，
    信号进度不前进，下一轮重试。
    """

    def __init__(
        self,
        *,
        recorder: MemoryRecorder,
        promotion: MemoryPromotion,
        maintenance: MemoryMaintenance,
        retriever: MemoryRetriever,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """初始化记忆 agent 的触发。

        Args:
            recorder: 记忆 agent 的编排。
            promotion: 会话结束时的晋升。
            maintenance: 会话结束时的休眠标记与整理。
            retriever: 用于取本会话涉及的文档。
            tenant_id: 当前课题组 ID。
            user_id: 当前用户 ID。

        Returns:
            None。
        """
        self.recorder = recorder  # 记忆 agent 的编排。
        self.promotion = promotion  # 会话结束时的晋升。
        self.maintenance = maintenance  # 会话结束时的维护。
        self.retriever = retriever  # 记忆读取，提供本会话涉及的文档。
        self.user_scope = MemoryScope("user", tenant_id, user_id)  # 当前用户级归属。
        self.tenant_scope = MemoryScope("tenant", tenant_id)  # 当前课题组级归属。
        self._executor = ThreadPoolExecutor(  # 串行运行记忆 agent 的后台线程。
            max_workers=1, thread_name_prefix="memory-agent"
        )
        self._pending: list[Future[None]] = []  # 本会话已提交、尚未等待的任务。
        self._session_started_at = datetime.now().astimezone()  # 本会话开始时间。
        self._changed: set[str] = set()  # 本会话有条目被写入或修改的级别。
        self._lock = threading.Lock()  # 保护 _changed。

    def register(self, hook_engine: HookEngine) -> None:
        """注册轮次收集与会话结束事件，失败只记日志。

        Args:
            hook_engine: Runtime 使用的 HookEngine。

        Returns:
            None。
        """
        TurnCollector(self._on_turn).register(hook_engine)
        hook_engine.register(
            HookEvent.SESSION_END, self._on_session_end, critical=False
        )

    def _on_turn(self, turn: TurnRecord) -> None:
        """成功的轮次提交给后台记忆 agent；失败轮次的信号留到下一轮处理。

        Args:
            turn: 轮次记录。

        Returns:
            None。
        """
        if turn.error:
            return
        papers = self.retriever.session_papers()  # 在主线程读取会话状态。
        self._pending.append(self._executor.submit(self._record, turn, papers))

    def _on_session_end(self, context: HookContext) -> None:
        """会话结束时的收尾，顺序见模块说明。

        Args:
            context: SESSION_END 事件上下文。

        Returns:
            None。
        """
        for future in self._pending:
            future.result()
        self._pending.clear()
        papers = self.retriever.session_papers()
        self._record(None, papers)
        with self._lock:
            changed = set(self._changed)
            self._changed.clear()
        since = self._session_started_at
        self._session_started_at = datetime.now().astimezone()

        if "user" in changed and _safely(
            "晋升课题组共性", self.promotion.promote_users, self.tenant_scope.tenant_id
        ):
            changed.add("tenant")
        for scope in (self.user_scope, self.tenant_scope):
            _safely("标记休眠", self.maintenance.mark_dormant, scope)
            if scope.level in changed:
                _safely(
                    "整理记忆",
                    lambda scope=scope: self.maintenance.consolidate(
                        scope, since=since
                    ),
                )
        if "tenant" in changed:
            _safely("生成通用候选", self.promotion.refresh_global_candidates)
        document_scopes = [
            MemoryScope(
                "document", self.user_scope.tenant_id, self.user_scope.user_id, paper_id
            )
            for paper_id, _ in papers
        ]
        for scope in (self.user_scope, self.tenant_scope, *document_scopes):
            _safely("归档治理", self.maintenance.archive, scope)

    def _record(
        self,
        turn: TurnRecord | None,
        papers: Sequence[tuple[str, str]],
    ) -> None:
        """调用记忆 agent 并记录有变化的级别；失败只记日志。

        Args:
            turn: 本轮对话；会话结束时为 None。
            papers: 本会话涉及的文档 (文档 ID, 标题)。

        Returns:
            None。
        """
        changed = _safely("记忆 agent", self.recorder.record, turn, papers)
        if changed:
            with self._lock:
                self._changed |= changed


def _safely(step: str, function: Callable[..., Any], *arguments: Any) -> Any:
    """执行一个记忆步骤，失败只记日志。

    Args:
        step: 步骤名称，用于日志。
        function: 要执行的函数。
        *arguments: 传给函数的参数。

    Returns:
        函数返回值；失败时为 None。
    """
    try:
        return function(*arguments)
    except Exception:  # noqa: BLE001 - 记忆失败不影响对话与后续步骤。
        _LOGGER.exception("记忆步骤失败: %s", step)
        return None
