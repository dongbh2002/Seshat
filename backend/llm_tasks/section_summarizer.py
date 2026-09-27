"""章节摘要子任务，按内容哈希把摘要缓存到磁盘，章节内容不变即复用。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openai import OpenAI

from backend.llm_tasks.base import LLMTask
from backend.templating import PromptRenderer


class SectionSummarizer(LLMTask):
    """为章节 Markdown 生成有字数上限的摘要。"""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        request_options: Mapping[str, Any],
        renderer: PromptRenderer,
        *,
        cache_path: Path,
        max_chars: int,
    ) -> None:
        """初始化章节摘要子任务并加载磁盘缓存。

        Args:
            client: OpenAI 兼容模型客户端。
            model: 实际模型名称。
            request_options: 每次请求附带的模型参数。
            renderer: 系统提示词模板渲染器。
            cache_path: 摘要缓存 JSON 文件路径。
            max_chars: 单个摘要的字符上限。

        Returns:
            None。
        """
        super().__init__(client, model, request_options, renderer)
        self.cache_path = cache_path  # 摘要缓存 JSON 文件路径。
        self.max_chars = max_chars  # 单个摘要的字符上限。
        self._cache: dict[str, str] = (  # 内容哈希到摘要的映射。
            json.loads(cache_path.read_text(encoding="utf-8"))
            if cache_path.is_file()
            else {}
        )
        self._lock = threading.Lock()  # 保护缓存读写。

    def summarize(self, markdown: str) -> str:
        """返回章节摘要，命中缓存时不调用模型。

        缓存键包含系统提示词，提示词或字数上限变化后自动失效。

        Args:
            markdown: 章节 Markdown。

        Returns:
            不超过 max_chars 的摘要文本。

        Raises:
            ValueError: 模型未返回有效摘要。
        """
        system_prompt = self.renderer.render(
            "section_summary_prompt.j2",
            max_chars=self.max_chars,
        )
        key = hashlib.sha256(f"{system_prompt}\n{markdown}".encode()).hexdigest()
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        summary = self._request_json(system_prompt, markdown).get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("模型未返回有效摘要")
        summary = summary.strip()[: self.max_chars]
        with self._lock:
            self._cache[key] = summary
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return summary
