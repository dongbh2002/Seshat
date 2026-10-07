"""记忆归纳子任务：找出多个下级范围中含义相同的规则，归纳为一条上一级规则。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.llm_tasks.base import LLMTask


class MemoryPromoter(LLMTask):
    """一次晋升对应一次独立模型调用；是否达到晋升门槛由调用方按范围数判断。"""

    def group(
        self,
        *,
        target_level: str,
        items: Sequence[Mapping[str, str]],
        existing: Sequence[Mapping[str, str]],
        item_max_chars: int,
    ) -> list[dict[str, Any]]:
        """归纳下级规则。

        Args:
            target_level: 目标级别：tenant 或 global。
            items: 待归纳的下级规则：label、scope（范围代号）、content。
            existing: 目标级已有记忆：id、status、content。
            item_max_chars: 单条记忆的字符上限。

        Returns:
            模型返回的分组对象列表（members、content、target），未经校验。

        Raises:
            ValueError: 模型未返回 JSON 对象或缺少 groups 数组。
        """
        system_prompt = self.renderer.render(
            "memory_promote_prompt.j2",
            target_level=target_level,
            item_max_chars=item_max_chars,
        )
        user_content = self.renderer.render(
            "memory_promote_input.j2",
            target_level=target_level,
            items=list(items),
            existing=list(existing),
        )
        groups = self._request_json(system_prompt, user_content).get("groups")
        if not isinstance(groups, list):
            raise ValueError("模型返回缺少 groups 数组")
        return [item for item in groups if isinstance(item, dict)]
