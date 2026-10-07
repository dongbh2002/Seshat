"""会话管理：持有当前会话，支持新建、恢复、列出会话，并在每轮结束后把会话状态写入会话文件。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from backend.hooks import HookContext, HookEngine, HookEvent, HookScope
from backend.session.repository import SessionRecord, SessionRepository


class SessionStateful(Protocol):
    """会话管理器依赖的运行时接口，由 Runtime 实现；session 包因此不依赖 runtime 包。"""

    def bind_session(self, session_id: str) -> None:
        """把后续运行归属到指定会话。

        Args:
            session_id: 会话 ID。

        Returns:
            None。
        """

    def reset(self) -> None:
        """清空全部会话状态。

        Returns:
            None。
        """

    def export_state(self) -> dict[str, Any]:
        """导出全部会话状态。

        Returns:
            可 JSON 序列化的会话状态。
        """

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """用 export_state 的导出结果替换全部会话状态。

        Args:
            state: export_state 导出的字典。

        Returns:
            None。
        """


class SessionManager:
    """维护当前会话；注册为 Hook 后每轮结束自动保存，失败的轮次同样保存已产生的审阅状态。

    离开会话（新开、恢复其他会话、关闭）前触发 SESSION_END，供记忆提炼等收尾处理。
    """

    save_events: ClassVar[tuple[HookEvent, ...]] = (  # 触发保存的生命周期事件。
        HookEvent.AFTER_RUNTIME,
        HookEvent.RUNTIME_ERROR,
    )

    def __init__(self, runtime: SessionStateful, repository: SessionRepository) -> None:
        """初始化会话管理器并新开一个会话；会话在首轮结束后才落盘。

        Args:
            runtime: 持有会话状态的运行时，应为刚创建、尚无状态的实例。
            repository: 会话文件仓库。

        Returns:
            None。
        """
        self.runtime = runtime  # 会话状态的实际持有者。
        self.repository = repository  # 会话文件读写。
        self.current = repository.create()  # 当前会话记录。
        self.hook_engine: HookEngine | None = None  # 注册后用于触发 SESSION_END。
        runtime.bind_session(self.current.id)

    def register(self, hook_engine: HookEngine) -> None:
        """注册到运行结束事件；保存失败不影响本轮回复。

        Args:
            hook_engine: Runtime 使用的 HookEngine，也用于触发 SESSION_END。

        Returns:
            None。
        """
        self.hook_engine = hook_engine
        for event in self.save_events:
            hook_engine.register(event, self, critical=False)

    def __call__(self, context: HookContext) -> None:
        """一轮运行结束后更新标题与轮数并保存当前会话。

        Args:
            context: AFTER_RUNTIME 或 RUNTIME_ERROR 事件上下文。

        Returns:
            None。
        """
        user_input = context.payload.get("user_input")
        if not self.current.title and isinstance(user_input, str):
            self.current.title = " ".join(user_input.split())
        if context.event == HookEvent.AFTER_RUNTIME:
            self.current.turn_count += 1
        self.save()

    def save(self) -> None:
        """把运行时的会话状态写入当前会话文件。

        Returns:
            None。

        Raises:
            OSError: 文件写入失败。
        """
        # TODO: 两个进程打开同一会话会互相覆盖，需要文件锁；保存失败目前只记日志，CLI 不提示。
        self.current.state = self.runtime.export_state()
        self.repository.save(self.current)

    def start_new(self) -> SessionRecord:
        """清空运行时状态并新开会话；旧会话已在每轮结束时保存。

        Returns:
            新会话记录。
        """
        self._end_current()
        self.runtime.reset()
        self.current = self.repository.create()
        self.runtime.bind_session(self.current.id)
        return self.current

    def resume(self, session_id: str) -> SessionRecord:
        """恢复已保存的会话，替换运行时的全部会话状态。

        Args:
            session_id: 会话 ID。

        Returns:
            恢复后的当前会话记录。

        Raises:
            FileNotFoundError: 会话不存在。
            ValueError: 会话 ID 或文件无效；状态无法恢复时已自动新开会话。
        """
        # TODO: 恢复后回显最近几轮对话；换模型后旧消息格式的兼容性尚未校验。
        record = self.repository.load(session_id)
        self._end_current()
        try:
            self.runtime.restore_state(record.state)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            new_record = self.start_new()
            raise ValueError(
                f"会话状态无法恢复: {session_id}（{error!r}），已新开会话 {new_record.id}"
            ) from error
        self.current = record
        self.runtime.bind_session(record.id)
        return record

    def close(self) -> None:
        """结束当前会话（程序退出时调用），触发 SESSION_END；会话状态已在每轮结束时保存。

        Returns:
            None。
        """
        self._end_current()

    def _end_current(self) -> None:
        """触发离开当前会话的 SESSION_END 事件；未注册时不触发。

        Returns:
            None。
        """
        if self.hook_engine is not None:
            self.hook_engine.emit(
                HookContext(
                    event=HookEvent.SESSION_END,
                    scope=HookScope(session_id=self.current.id),
                )
            )

    def list_sessions(self) -> list[SessionRecord]:
        """列出已保存的会话。

        Returns:
            按最后保存时间从新到旧排序的会话记录。
        """
        return self.repository.list_records()
