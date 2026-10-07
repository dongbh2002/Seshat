"""上下文引擎，负责组装最终上下文；压缩、审阅状态渲染与 token 估算由注入的组件负责。

消息顺序按变化频率排列，便于模型端前缀缓存：
[稳定指令 + 历史摘要] → 保留的历史 → 当前轮次 → 审阅工作状态（每步重建，不写入历史）
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from backend.memory import MemorySource
from backend.runtime.context.compression import (
    CompressionStrategy,
    Context,
    HistoryArchive,
)
from backend.runtime.context.review_state import ReviewStateContext
from backend.runtime.context.token_estimator import TokenEstimator
from backend.templating import PromptRenderer


class ContextOverflowError(RuntimeError):
    """压缩后的请求仍超出模型物理上限（窗口减去输出预留）。"""


@dataclass
class ContextBuild:
    """一次上下文组装的结果。"""

    messages: Context  # 可直接发送给模型的消息。
    history: Context  # 压缩后的历史消息，调用方写回作为下一步输入。
    active_turn: Context  # 压缩后的当前轮次消息，调用方写回作为下一步输入。
    estimated_tokens: int  # 消息与工具定义的 token 估算总量。
    report: dict[str, Any]  # 本次组装与压缩的统计，供日志记录。


class ContextEngine:
    """按系统、画像、记忆、历史和审阅状态职责组装模型上下文。"""

    def __init__(
        self,
        renderer: PromptRenderer,
        compression: CompressionStrategy,
        review_state_context: ReviewStateContext,
        estimator: TokenEstimator,
        physical_limit_tokens: int,
        memory: MemorySource | None,
    ) -> None:
        """渲染系统提示词并初始化上下文引擎。

        Args:
            renderer: 提示词模板渲染器。
            compression: 历史与当前轮次的压缩策略。
            review_state_context: 审阅工作状态的渲染器。
            estimator: token 估算器。
            physical_limit_tokens: 请求允许的 token 上限（模型窗口减输出预留）。
            memory: 画像与长期记忆的来源；未启用记忆（游客、评估）时为 None。

        Returns:
            None。

        Raises:
            FileNotFoundError: 系统提示词模板不存在。
            ValueError: 系统提示词渲染后为空。
        """
        self.renderer = renderer  # 所有上下文模板的渲染器。
        self.compression = compression  # 可替换的上下文压缩策略。
        self.review_state_context = review_state_context  # 审阅工作状态渲染器。
        self.estimator = estimator  # token 估算器。
        self.physical_limit_tokens = physical_limit_tokens  # 请求的物理 token 上限。
        self.memory = memory  # 画像与长期记忆的来源。
        self.archive = HistoryArchive.empty()  # 已归档轮次，摘要只追加不重算。
        self.system_prompt = renderer.render("system_prompt.j2")  # 基础系统提示词。
        if not self.system_prompt:
            raise ValueError("系统提示词模板渲染结果为空: system_prompt.j2")

    def get_final_context(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
        *,
        tool_definitions: Sequence[Mapping[str, Any]],
    ) -> ContextBuild:
        """组合指令、历史摘要、保留的历史、当前轮次和末尾的审阅工作状态。

        组装前先把审阅状态与磁盘上的文档同步，使会话外修改在本步的压缩与
        审阅状态中立即生效。

        Args:
            history: 已完成轮次的消息（上一步压缩后的结果），不含 system 消息。
            active_turn: 当前用户输入及本轮产生的模型和工具消息。
            tool_definitions: 本次请求附带的工具定义，计入 token 预算。

        Returns:
            最终消息、调用方需写回的压缩后历史与当前轮，以及 token 估算。

        Raises:
            ContextOverflowError: 压缩后仍超出模型物理上限。
        """
        self.review_state_context.sync_documents()
        memory = self.memory.select_memory() if self.memory is not None else {}
        base_instruction = self._merge_instruction_contexts(
            self.system_prompt,
            self._get_profile_context(memory),
            self._get_memory_context(memory),
        )
        review_state = self.review_state_context.render(
            unrecorded_reads=[],
            forced_reads=[],
        )
        tools_tokens = self.estimator.estimate(list(tool_definitions))
        reserved_tokens = (
            self.estimator.estimate({"role": "system", "content": base_instruction})
            + self.estimator.estimate({"role": "system", "content": review_state})
            + tools_tokens
        )
        result = self.compression.compress(
            history,
            active_turn,
            archive=self.archive,
            reserved_tokens=reserved_tokens,
        )
        self.archive = result.archive
        if result.unrecorded_reads or result.forced_reads:
            review_state = self.review_state_context.render(
                unrecorded_reads=result.unrecorded_reads,
                forced_reads=result.forced_reads,
            )
        messages = [
            {
                "role": "system",
                "content": self._merge_instruction_contexts(
                    base_instruction,
                    result.history_summary,
                ),
            },
            *result.history,
            *result.active_turn,
            {"role": "system", "content": review_state},
        ]
        estimated_tokens = self.estimator.estimate_messages(messages) + tools_tokens
        if estimated_tokens > self.physical_limit_tokens:
            raise ContextOverflowError(
                f"上下文估算 {estimated_tokens} tokens，超过物理上限 "
                f"{self.physical_limit_tokens}（模型窗口减输出预留）；"
                "通常是单次工具结果过大，请缩小读取范围。"
            )
        return ContextBuild(
            messages=messages,
            history=result.history,
            active_turn=result.active_turn,
            estimated_tokens=estimated_tokens,
            report={
                **asdict(result.stats),
                "estimated_tokens": estimated_tokens,
                "reserved_tokens": reserved_tokens,
                "tools_tokens": tools_tokens,
                "chars_per_token": round(self.estimator.chars_per_token, 3),
                "archived_message_count": result.archived_message_count,
                "unrecorded_reads": len(result.unrecorded_reads),
                "forced_reads": len(result.forced_reads),
            },
        )

    def reset(self) -> None:
        """清空上下文引擎持有的会话状态：审阅状态与历史归档。

        Returns:
            None。
        """
        self.review_state_context.reset()
        self.archive = HistoryArchive.empty()

    def export_state(self) -> dict[str, Any]:
        """导出上下文引擎持有的会话状态，供会话持久化。

        Returns:
            ``archive``：历史归档的字段字典；``review_state``：审阅状态。
        """
        return {
            "archive": asdict(self.archive),
            "review_state": self.review_state_context.export_state(),
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """用 ``export_state`` 的导出结果替换会话状态。

        Args:
            state: ``export_state`` 导出的字典。

        Returns:
            None。
        """
        self.archive = HistoryArchive(**copy.deepcopy(dict(state["archive"])))
        self.review_state_context.restore_state(state["review_state"])

    def _get_profile_context(self, memory: Mapping[str, Any]) -> str:
        """获取当前服务对象的研究与写作画像上下文。

        Args:
            memory: 本步选取的记忆，``profile`` 为用户级事实；未启用记忆时为空。

        Returns:
            已渲染的画像提示词。
        """
        return self.renderer.render(
            "profile_prompt.j2", profile=memory.get("profile", [])
        )

    def _get_memory_context(self, memory: Mapping[str, Any]) -> str:
        """获取当前会话的长期记忆上下文。

        Args:
            memory: 本步选取的记忆（documents、advisor、tenant、user、general）；未启用记忆时为空。

        Returns:
            已渲染的记忆提示词；没有记忆时为空字符串。
        """
        # TODO: 记忆增多后按当前任务做相关性检索，而不只按支持数截断。
        return self.renderer.render(
            "memory_prompt.j2",
            documents=memory.get("documents", []),
            advisor=memory.get("advisor", []),
            tenant=memory.get("tenant", []),
            user=memory.get("user", []),
            general=memory.get("general", []),
        )

    @staticmethod
    def _merge_instruction_contexts(*contexts: str) -> str:
        """将非空的指令上下文合并为一条指令。

        Args:
            *contexts: 按优先顺序排列的指令上下文。

        Returns:
            以空行分隔的非空指令文本。
        """
        return "\n\n".join(context.strip() for context in contexts if context.strip())
