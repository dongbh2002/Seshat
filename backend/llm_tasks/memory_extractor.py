"""记忆 agent 的模型调用：把本轮对话、本轮新信号与现有记忆交给模型，由它决定新增、强化、修订或推翻哪些记忆。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.llm_tasks.base import LLMTask


class MemoryExtractor(LLMTask):
    """一轮（或一批信号）对应一次独立模型调用；返回的操作由调用方校验后执行。"""

    def extract(
        self,
        *,
        dialogue: Mapping[str, Any] | None,
        signals: Sequence[Mapping[str, Any]],
        papers: Sequence[Mapping[str, str]],
        existing: Sequence[Mapping[str, str]],
        item_max_chars: int,
        excerpt_chars: int,
        dialogue_chars: int,
    ) -> list[dict[str, Any]]:
        """决定本轮要写入的记忆。

        Args:
            dialogue: 本轮对话：user_input、reply、tool_calls（name、arguments）；
                会话结束时处理遗留信号为 None。
            signals: 带编号（label）的信号字段，键同 Signal 并含 paper（文档编号）。
            papers: 可写入文档级记忆的文档：label、title。
            existing: 现有记忆：id、level、paper、kind、content。
            item_max_chars: 单条记忆的字符上限。
            excerpt_chars: 每条修改片段展示的字符上限。
            dialogue_chars: 用户输入与回复各自展示的字符上限。

        Returns:
            模型返回的操作对象列表，未经校验；没有值得记的内容时为空。

        Raises:
            ValueError: 模型未返回 JSON 对象或缺少 operations 数组。
        """
        system_prompt = self.renderer.render(
            "memory_extract_prompt.j2",
            papers=list(papers),
            existing=list(existing),
            item_max_chars=item_max_chars,
        )
        user_content = self.renderer.render(
            "memory_extract_input.j2",
            dialogue=dialogue,
            signals=list(signals),
            excerpt_chars=excerpt_chars,
            dialogue_chars=dialogue_chars,
        )
        operations = self._request_json(system_prompt, user_content).get("operations")
        if not isinstance(operations, list):
            raise ValueError("模型返回缺少 operations 数组")
        return [item for item in operations if isinstance(item, dict)]
