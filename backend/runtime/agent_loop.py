"""Agent 运行循环，负责维护多轮消息并执行模型发起的工具调用。"""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from backend.config import config
from backend.runtime.tool_engine import ToolEngine


class AgentLoop:
    """维护对话历史，在模型回复与本地工具之间循环。"""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        tool_engine: ToolEngine | None = None,
        system_prompt: str | None = None,
        max_steps: int = 8,
        default_request_options: dict[str, Any] | None = None,
    ) -> None:
        """初始化 Agent 循环。

        Args:
            client: OpenAI 兼容模型客户端。
            model: 每轮请求使用的实际模型名称。
            tool_engine: 可选的工具注册与执行引擎。
            system_prompt: 可选的系统提示词。
            max_steps: 单次用户输入允许的最大模型调用轮数。
            default_request_options: 每次模型调用默认使用的请求参数。

        Returns:
            None。
        """
        self.client = client  # 模型客户端，由 providers 创建。
        self.model = model  # 当前 Agent 使用的实际模型名称。
        self.tool_engine = tool_engine  # 工具定义与执行入口。
        self.max_steps = max_steps  # 防止模型持续调用工具而无法结束。
        self.system_prompt = system_prompt  # 新会话需要保留的系统提示词。
        model_config = config["models"][config["current_model"]]
        configured_parameters = model_config.get("parameters", {})
        self.default_request_options = dict(
            configured_parameters
            if default_request_options is None
            else default_request_options
        )  # 当前模型的默认参数，单次调用参数可以覆盖它。
        self.messages: list[dict[str, Any]] = []  # 跨轮保存的完整对话历史。

        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def run(self, user_input: str, **request_options: Any) -> str:
        """处理一条用户输入，直到模型返回最终文本。

        Args:
            user_input: 本轮用户消息。
            **request_options: 透传给 Chat Completions API 的其他参数。

        Returns:
            模型结束工具调用后返回的最终文本。

        Raises:
            RuntimeError: 工具调用循环超过 ``max_steps``。
        """
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(self.max_steps):
            options = {**self.default_request_options, **request_options}
            if self.tool_engine:
                definitions = self.tool_engine.get_definitions()
                if definitions:
                    options["tools"] = definitions

            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,  # type: ignore[arg-type]
                **options,
            )
            message = response.choices[0].message
            assistant_message = message.model_dump(exclude_none=True)
            assistant_message["role"] = "assistant"
            self.messages.append(assistant_message)

            if not message.tool_calls:
                return message.content or ""

            for tool_call in message.tool_calls:
                self.messages.append(self._execute_tool(tool_call))

        raise RuntimeError(f"Agent 工具调用超过最大轮数: {self.max_steps}")

    def reset(self) -> None:
        """清空当前对话历史。

        Returns:
            None。
        """
        self.messages.clear()
        if self.system_prompt:
            self.messages.append({"role": "system", "content": self.system_prompt})

    def _execute_tool(self, tool_call: Any) -> dict[str, Any]:
        """执行一次模型请求的本地工具调用。

        Args:
            tool_call: OpenAI SDK 返回的 function tool call 对象。

        Returns:
            可追加到对话历史的 ``role=tool`` 消息。

        Raises:
            RuntimeError: 当前 Agent 没有配置工具引擎。
            ValueError: 工具参数不是 JSON 对象。
        """
        if not self.tool_engine:
            raise RuntimeError("Agent 未配置工具引擎")

        tool_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError(f"工具参数必须是 JSON 对象: {tool_name}")

        result = self.tool_engine.execute(tool_name, arguments)
        content = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, default=str)
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": content,
        }
