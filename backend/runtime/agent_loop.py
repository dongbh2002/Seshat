"""Agent 运行循环，负责维护多轮消息并执行模型发起的工具调用。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from openai import OpenAI

from backend.config import Settings
from backend.runtime.context_engine import ContextEngine
from backend.runtime.hook_engine import (
    HookContext,
    HookEngine,
    HookEvent,
    HookExecutionError,
    HookScope,
)
from backend.runtime.tool_engine import ToolEngine


class AgentLoop:
    """维护对话历史，在模型回复与本地工具之间循环。"""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        tool_engine: ToolEngine,
        context_engine: ContextEngine,
        hook_engine: HookEngine,
        settings: Settings,
    ) -> None:
        """初始化 Agent 循环。

        Args:
            client: OpenAI 兼容模型客户端。
            model: 每轮请求使用的实际模型名称。
            tool_engine: 由外部装配并负责工具定义和执行的引擎。
            context_engine: 由外部装配并负责模型上下文的引擎。
            hook_engine: 由外部装配并负责模型生命周期事件的引擎。
            settings: 启动入口加载并注入的类型化项目配置。

        Returns:
            None。
        """
        self.client = client  # 模型客户端，由 providers 创建。
        self.model = model  # 当前 Agent 使用的实际模型名称。
        self.tool_engine = tool_engine  # 外部装配的工具定义与执行入口。
        self.context_engine = context_engine  # 外部装配的模型上下文组装入口。
        self.hook_engine = hook_engine  # 外部装配的模型生命周期 Hook 入口。
        self.max_steps = (  # 单次用户输入的最大模型调用轮数。
            settings.agent_loop.max_steps
        )
        self.default_request_options = (  # 当前模型的默认请求参数。
            dict(settings.active_model.parameters)
        )
        self.messages: list[dict[str, Any]] = []  # 已完成轮次的原始对话历史。

    def run(
        self,
        user_input: str,
        *,
        hook_scope: HookScope | None = None,
        **request_options: Any,
    ) -> str:
        """处理一轮用户输入，持续执行模型和工具直到返回文本。

        Args:
            user_input: 当前轮次的用户输入。
            hook_scope: 可选的租户、用户、会话和运行范围。
            **request_options: 透传给 Chat Completions API 的其他参数。

        Returns:
            模型结束工具调用后返回的最终文本。

        Raises:
            TypeError: 用户输入不是字符串。
            RuntimeError: 工具调用循环超过 ``max_steps``。
        """
        if not isinstance(user_input, str):
            raise TypeError("user_input 必须是字符串")

        scope = hook_scope or HookScope()
        active_turn: list[dict[str, Any]] = [{"role": "user", "content": user_input}]

        for step_index in range(self.max_steps):
            model_messages = self.context_engine.get_final_context(
                history=self.messages,
                active_turn=active_turn,
            )
            options = copy.deepcopy({**self.default_request_options, **request_options})
            definitions = self.tool_engine.get_definitions()
            if definitions:
                options["tools"] = definitions

            metadata: dict[str, Any] = {"step": step_index + 1}
            payload = {
                "model": self.model,
                "messages": copy.deepcopy(model_messages),
                "request_options": options,
            }
            executed_payload = payload
            try:
                before_context = self._emit_hook(
                    HookEvent.BEFORE_MODEL_REQUEST,
                    scope=scope,
                    payload=payload,
                    metadata=metadata,
                )
                executed_payload = before_context.payload
                model = before_context.payload.get("model")
                hooked_messages = before_context.payload.get("messages")
                hooked_options = before_context.payload.get("request_options")
                if not isinstance(model, str) or not model:
                    raise ValueError("before_model_request 必须保留非空字符串 model")
                if not isinstance(hooked_messages, list) or not all(
                    isinstance(message, Mapping) for message in hooked_messages
                ):
                    raise TypeError(
                        "before_model_request 必须保留消息对象列表 messages"
                    )
                if not isinstance(hooked_options, Mapping):
                    raise TypeError(
                        "before_model_request 必须保留对象类型 request_options"
                    )

                response = self.client.chat.completions.create(
                    model=model,
                    messages=[dict(message) for message in hooked_messages],  # type: ignore[arg-type]
                    **dict(hooked_options),
                )
                message = response.choices[0].message
                assistant_message = message.model_dump(exclude_none=True)
                assistant_message["role"] = "assistant"
                usage = getattr(response, "usage", None)
                usage_data = (
                    usage.model_dump(exclude_none=True)
                    if hasattr(usage, "model_dump")
                    else usage
                )
                response_result = {
                    "response_id": getattr(response, "id", None),
                    "model": getattr(response, "model", model),
                    "finish_reason": getattr(
                        response.choices[0],
                        "finish_reason",
                        None,
                    ),
                    "message": assistant_message,
                    "usage": usage_data,
                }
                self._emit_hook(
                    HookEvent.AFTER_MODEL_RESPONSE,
                    scope=scope,
                    payload=executed_payload,
                    metadata=metadata,
                    result=response_result,
                )
            except Exception as error:
                try:
                    self._emit_hook(
                        HookEvent.MODEL_ERROR,
                        scope=scope,
                        payload=executed_payload,
                        metadata=metadata,
                        error=error,
                    )
                except Exception as hook_error:
                    raise hook_error from error
                raise

            active_turn.append(assistant_message)

            if not message.tool_calls:
                self.messages.extend(active_turn)
                return message.content or ""

            for tool_call in message.tool_calls:
                active_turn.append(
                    self._execute_tool(
                        tool_call,
                        hook_scope=scope,
                        step=step_index + 1,
                    )
                )

        raise RuntimeError(f"Agent 工具调用超过最大轮数: {self.max_steps}")

    def reset(self) -> None:
        """清空当前对话历史。

        Returns:
            None。
        """
        self.messages.clear()
        # TODO: 滚动历史摘要接入后，同步重置 ContextEngine 的会话状态。

    def _execute_tool(
        self,
        tool_call: Any,
        *,
        hook_scope: HookScope,
        step: int,
    ) -> dict[str, Any]:
        """执行一次模型请求的本地工具调用。

        Args:
            tool_call: OpenAI SDK 返回的 function tool call 对象。
            hook_scope: 当前租户、用户、会话和 Runtime 运行范围。
            step: 当前模型调用在本次 Agent 循环中的步骤序号。

        Returns:
            可追加到对话历史的 ``role=tool`` 消息。

        Raises:
            HookExecutionError: 关键 Hook 自身执行失败。
        """
        tool_name = tool_call.function.name
        raw_arguments = tool_call.function.arguments or "{}"
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise TypeError(f"工具参数必须是 JSON 对象: {tool_name}")
        except Exception as error:  # noqa: BLE001 - 参数错误需返回模型自行纠正。
            self._emit_hook(
                HookEvent.TOOL_CALL_ERROR,
                scope=hook_scope,
                payload={"name": tool_name, "arguments": raw_arguments},
                metadata={"step": step, "tool_call_id": tool_call.id},
                error=error,
            )
            content = self._build_tool_error_content(tool_name, error)
            return {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": content,
            }

        try:
            result = self.tool_engine.execute(
                tool_name,
                arguments,
                hook_scope=hook_scope,
                hook_metadata={
                    "step": step,
                    "tool_call_id": tool_call.id,
                },
            )
            content = (
                result
                if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False, default=str)
            )
        except HookExecutionError:
            raise
        except Exception as error:  # noqa: BLE001 - 工具错误需返回模型以便纠正。
            content = self._build_tool_error_content(tool_name, error)
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": content,
        }

    @staticmethod
    def _build_tool_error_content(tool_name: str, error: BaseException) -> str:
        """生成可返回给模型的统一工具错误 JSON。

        Args:
            tool_name: 当前调用的工具名称。
            error: 工具参数解析或执行过程中产生的异常。

        Returns:
            包含错误类型、错误说明和重试建议的 JSON 字符串。
        """
        return json.dumps(
            {
                "ok": False,
                "tool": tool_name,
                "error_type": type(error).__name__,
                "error": str(error),
                "suggestion": "请根据错误信息修正参数后重试。",
            },
            ensure_ascii=False,
        )

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
        """构造模型生命周期上下文，并在已配置时触发 Hook。

        Args:
            event: 当前模型生命周期事件。
            scope: 当前运行范围。
            payload: 模型、消息和请求参数。
            metadata: 当前模型步骤中各 Hook 共享的扩展数据。
            result: 模型成功返回的助手消息副本。
            error: 模型请求或模型 Hook 失败时的异常。

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
        self.hook_engine.emit(context)
        return context
