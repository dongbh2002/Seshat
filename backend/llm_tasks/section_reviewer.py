"""章节审阅子任务：在独立上下文中审阅单个章节，只返回定位到内容块的问题。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from backend.llm_tasks.base import LLMTask


class SectionReviewer(LLMTask):
    """map-reduce 中的 map 步骤：一个章节对应一次独立模型调用。"""

    def review(
        self,
        markdown: str,
        *,
        focus: str,
        decisions: Sequence[str],
        memory: Sequence[Mapping[str, str]],
    ) -> list[dict[str, str]]:
        """审阅一个章节。

        Args:
            markdown: 章节 Markdown，每个内容块带 [ID] 标记。
            focus: 本次审阅要求。
            decisions: 用户已确认的审阅决定。
            memory: 长期记忆中的要求（level、content），按优先级排列；未启用记忆时为空。

        Returns:
            ``{"block_id", "issue"}`` 列表；格式不合法的条目被丢弃。

        Raises:
            ValueError: 模型未返回 JSON 对象。
        """
        system_prompt = self.renderer.render(
            "section_review_prompt.j2",
            focus=focus,
            decisions=list(decisions),
            memory=list(memory),
        )
        findings = self._request_json(system_prompt, markdown).get("findings")
        if not isinstance(findings, list):
            raise ValueError("模型返回缺少 findings 数组")
        return [
            {"block_id": item["block_id"], "issue": item["issue"].strip()}
            for item in findings
            if isinstance(item, dict)
            and isinstance(item.get("block_id"), str)
            and isinstance(item.get("issue"), str)
            and item["issue"].strip()
        ]
