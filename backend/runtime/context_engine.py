"""上下文引擎，负责组装最终上下文并提供上下文压缩入口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

Context = list[dict[str, Any]]


class ContextEngine:
    """统一管理静态上下文、动态上下文及压缩流程。"""

    def get_final_context(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> Context:
        """组合静态上下文与动态上下文。

        Args:
            messages: 当前会话的原始消息列表。

        Returns:
            可直接提供给模型的最终上下文。
        """
        static_context = self._get_static_context()
        dynamic_context = self._get_dynamic_context(messages)
        return static_context + dynamic_context

    def compress(self, context: Sequence[Mapping[str, Any]]) -> Context:
        """压缩上下文以减少后续模型请求的 token 使用量。

        Args:
            context: 需要压缩的上下文消息列表。

        Returns:
            压缩后的上下文；当前暂时原样复制返回。
        """
        # TODO: 根据模型上下文窗口、保留优先级和摘要策略实现压缩。
        return [dict(message) for message in context]

    def _get_static_context(self) -> Context:
        """获取不随会话轮次变化的静态上下文。

        Returns:
            静态上下文消息；当前暂时返回空列表。
        """
        # TODO: 明确系统提示词、工具说明和租户配置等静态来源。
        return []

    def _get_dynamic_context(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> Context:
        """获取随会话变化的动态上下文。

        Args:
            messages: 当前会话的原始消息列表。

        Returns:
            动态上下文消息；当前暂时原样复制返回。
        """
        # TODO: 明确对话历史、记忆和当前任务信息的筛选规则。
        return [dict(message) for message in messages]
