"""记忆整理子任务：审视一个归属下的全部记忆，返回合并重复、下线被推翻条目、改写过于具体条目的操作。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.llm_tasks.base import LLMTask


class MemoryConsolidator(LLMTask):
    """一个归属对应一次独立模型调用；返回的操作由调用方校验后执行。"""

    def consolidate(
        self,
        *,
        level: str,
        items: Sequence[Mapping[str, Any]],
        item_max_chars: int,
    ) -> list[dict[str, Any]]:
        """整理一个归属下的记忆。

        Args:
            level: 被整理的级别：user 或 tenant。
            items: 生效与休眠条目：id、kind、source、support、against、last_supported、dormant、content。
            item_max_chars: 单条记忆的字符上限。

        Returns:
            模型返回的操作对象列表（merge、retire、rewrite），未经校验。

        Raises:
            ValueError: 模型未返回 JSON 对象或缺少 operations 数组。
        """
        system_prompt = self.renderer.render(
            "memory_consolidate_prompt.j2",
            level=level,
            item_max_chars=item_max_chars,
        )
        user_content = self.renderer.render(
            "memory_consolidate_input.j2",
            level=level,
            items=list(items),
        )
        operations = self._request_json(system_prompt, user_content).get("operations")
        if not isinstance(operations, list):
            raise ValueError("模型返回缺少 operations 数组")
        return [item for item in operations if isinstance(item, dict)]
