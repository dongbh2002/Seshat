"""LLM 子任务基类：用独立的单次模型调用处理一段内容，不共享主对话上下文。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from openai import OpenAI

from backend.templating import PromptRenderer


class LLMTask:
    """封装子任务共用的模板渲染、模型请求和 JSON 结果解析。"""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        request_options: Mapping[str, Any],
        renderer: PromptRenderer,
    ) -> None:
        """初始化子任务。

        Args:
            client: OpenAI 兼容模型客户端。
            model: 实际模型名称。
            request_options: 每次请求附带的模型参数。
            renderer: 子任务系统提示词的模板渲染器。

        Returns:
            None。
        """
        self.client = client  # 模型客户端。
        self.model = model  # 子任务使用的模型名称。
        self.request_options = dict(request_options)  # 模型请求参数。
        self.renderer = renderer  # 系统提示词模板渲染器。
        # TODO: 接入 HookEngine，使子任务的模型调用也进入生命周期日志。

    def _request_json(self, system_prompt: str, user_content: str) -> dict[str, Any]:
        """发起一次独立模型调用并解析返回的 JSON 对象。

        Args:
            system_prompt: 子任务系统提示词。
            user_content: 子任务处理的内容。

        Returns:
            模型输出中的 JSON 对象。

        Raises:
            ValueError: 模型输出不包含合法的 JSON 对象。
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            **copy.deepcopy(self.request_options),
        )
        content = response.choices[0].message.content or ""
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("模型未返回 JSON 对象")
        data = json.loads(content[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("模型返回的 JSON 不是对象")
        return data
