"""Runtime 顶层编排器，串联上下文处理与 Agent 多轮交互。"""

from __future__ import annotations

from typing import Any

from backend.runtime.agent_loop import AgentLoop
from backend.runtime.context_engine import ContextEngine


class Runtime:
    """协调上下文引擎和 Agent 循环，提供统一的对话运行入口。"""

    def __init__(
        self,
        agent_loop: AgentLoop,
        context_engine: ContextEngine,
    ) -> None:
        """初始化运行时所需组件。

        Args:
            agent_loop: 负责模型请求和工具调用循环的 Agent。
            context_engine: 负责上下文组装和压缩的引擎。

        Returns:
            None。
        """
        self.agent_loop = agent_loop  # 模型与工具调用循环。
        self.context_engine = context_engine  # 上下文组装与压缩入口。

    def run(self, user_input: str, **request_options: Any) -> str:
        """执行一轮完整交互，并保留消息供后续轮次继续使用。

        交互流程为：整理历史上下文、压缩上下文、加入用户输入、调用模型、
        执行模型请求的工具，最后返回模型文本回复。

        Args:
            user_input: 当前轮次的用户输入。
            **request_options: 透传给模型请求的可选参数。

        Returns:
            当前轮次模型生成的最终文本回复。
        """
        context = self.context_engine.get_final_context(self.agent_loop.messages)
        compressed_context = self.context_engine.compress(context)

        # TODO: 静态上下文启用后，应由 ContextEngine 唯一维护，避免重复注入。
        self.agent_loop.messages = compressed_context

        return self.agent_loop.run(user_input, **request_options)
