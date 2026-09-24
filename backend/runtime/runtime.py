"""Runtime 顶层入口，处理运行范围和 Agent 调用生命周期。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from backend.config import config
from backend.runtime.agent_loop import AgentLoop
from backend.runtime.context_engine import ContextEngine
from backend.runtime.hook_engine import (
    HookContext,
    HookEngine,
    HookEvent,
    HookScope,
)


def _get_default_hook_scope() -> HookScope:
    """从当前项目配置生成默认租户、用户和会话 Hook 范围。

    Returns:
        不包含 run_id 的默认 HookScope。
    """
    tenant_value = config.get("default_tenant")
    tenant_id = str(tenant_value) if tenant_value is not None else None
    tenants = config.get("tenants", {})
    tenant_config = (
        tenants.get(tenant_id, {})
        if tenant_id is not None and isinstance(tenants, Mapping)
        else {}
    )
    if not isinstance(tenant_config, Mapping):
        tenant_config = {}
    user_value = tenant_config.get("default_user")
    session_value = tenant_config.get("session_name")
    return HookScope(
        tenant_id=tenant_id,
        user_id=str(user_value) if user_value is not None else None,
        session_name=str(session_value) if session_value is not None else None,
    )


class Runtime:
    """处理用户输入和运行级 Hook，提供统一的对话入口。"""

    def __init__(
        self,
        agent_loop: AgentLoop,
        context_engine: ContextEngine | None = None,
        hook_engine: HookEngine | None = None,
        hook_scope: HookScope | None = None,
    ) -> None:
        """初始化运行时所需组件。

        Args:
            agent_loop: 负责模型请求和工具调用循环的 Agent。
            context_engine: 可选的上下文引擎；提供时注入 AgentLoop。
            hook_engine: Runtime、模型和工具共享的可选 HookEngine。
            hook_scope: 可选的租户、用户和会话范围；默认从配置读取。

        Returns:
            None。
        """
        self.agent_loop = agent_loop  # 模型与工具调用循环。
        if context_engine is not None:
            self.agent_loop.context_engine = context_engine
        self.hook_engine = hook_engine or agent_loop.hook_engine  # 共享 Hook 入口。
        self.hook_scope = hook_scope or _get_default_hook_scope()  # 基础运行范围。
        if self.hook_engine is not None:
            self.agent_loop.hook_engine = self.hook_engine
            self.agent_loop.tool_engine.hook_engine = self.hook_engine

    def run(self, user_input: str, **request_options: Any) -> str:
        """执行一轮完整交互，并保留消息供后续轮次继续使用。

        AgentLoop 会在每次模型请求前调用 ContextEngine，包括工具
        返回结果后发起的后续模型请求。

        Args:
            user_input: 当前轮次的用户输入。
            **request_options: 透传给模型请求的可选参数。

        Returns:
            当前轮次模型生成的最终文本回复。
        """
        scope = self.hook_scope.with_run_id(uuid4().hex)
        metadata: dict[str, Any] = {}
        payload = {
            "user_input": user_input,
            "request_options": copy.deepcopy(dict(request_options)),
        }
        try:
            before_context = self._emit_hook(
                HookEvent.BEFORE_RUNTIME,
                scope=scope,
                payload=payload,
                metadata=metadata,
            )
            hooked_user_input = before_context.payload.get("user_input")
            hooked_options = before_context.payload.get("request_options")
            if not isinstance(hooked_user_input, str):
                raise TypeError("before_runtime 必须保留字符串类型 user_input")
            if not isinstance(hooked_options, Mapping):
                raise TypeError("before_runtime 必须保留对象类型 request_options")

            result = self.agent_loop.run(
                hooked_user_input,
                hook_scope=scope,
                **dict(hooked_options),
            )
            self._emit_hook(
                HookEvent.AFTER_RUNTIME,
                scope=scope,
                payload=before_context.payload,
                metadata=metadata,
                result=result,
            )
            return result
        except Exception as error:
            try:
                self._emit_hook(
                    HookEvent.RUNTIME_ERROR,
                    scope=scope,
                    payload=payload,
                    metadata=metadata,
                    error=error,
                )
            except Exception as hook_error:
                raise hook_error from error
            raise

    def _emit_hook(
        self,
        event: HookEvent,
        *,
        scope: HookScope,
        payload: dict[str, Any],
        metadata: dict[str, Any],
        result: Any = None,
        error: BaseException | None = None,
    ) -> HookContext:
        """构造 Runtime 生命周期上下文，并在已配置时触发 Hook。

        Args:
            event: 当前 Runtime 生命周期事件。
            scope: 当前租户、用户、会话和 run_id。
            payload: 用户输入和模型请求参数。
            metadata: 当前 Runtime.run 中各 Hook 共享的扩展数据。
            result: Runtime 成功得到的最终文本回复。
            error: Runtime 或内部组件失败时的异常。

        Returns:
            Hook 执行后的上下文；未配置 HookEngine 时原样返回。
        """
        context = HookContext(
            event=event,
            scope=scope,
            payload=payload,
            metadata=metadata,
            result=result,
            error=error,
        )
        if self.hook_engine is not None:
            self.hook_engine.emit(context)
        return context
