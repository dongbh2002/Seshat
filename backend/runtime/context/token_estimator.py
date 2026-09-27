"""token 估算：按“字符数 ÷ 比例”估算消息 token，并用模型响应的 usage 持续校准比例。"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from backend.config import BudgetSettings
from backend.hooks import HookContext, HookEngine, HookEvent


class TokenEstimator:
    """以紧凑 JSON 字符数估算 token；比例由 TokenCalibrationHook 按 usage 校准。"""

    def __init__(self, settings: BudgetSettings) -> None:
        """以配置中的初始比例初始化估算器。

        Args:
            settings: 上下文预算配置。

        Returns:
            None。
        """
        self.chars_per_token = settings.initial_chars_per_token  # 当前字符/token 比例。
        self.calibration_weight = settings.calibration_weight  # 新观测值的权重。
        self._lock = threading.Lock()  # 校准与估算可能跨线程。

    @staticmethod
    def measure_chars(value: Any) -> int:
        """返回对象序列化为紧凑 JSON 后的字符数；字符串直接取长度。

        Args:
            value: 消息、消息列表、工具定义或文本。

        Returns:
            字符数。
        """
        if isinstance(value, str):
            return len(value)
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        )

    def estimate(self, value: Any) -> int:
        """估算单个对象的 token 数。

        Args:
            value: 消息、工具定义或文本。

        Returns:
            向上取整的 token 估算值。
        """
        with self._lock:
            ratio = self.chars_per_token
        return math.ceil(self.measure_chars(value) / ratio)

    def estimate_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        """逐条估算并求和，与压缩策略的逐条累计口径一致。

        Args:
            messages: 模型消息列表。

        Returns:
            token 估算值之和。
        """
        return sum(self.estimate(dict(message)) for message in messages)

    def calibrate(self, observed_chars: int, prompt_tokens: int) -> None:
        """用一次请求的实际 prompt token 数更新比例（指数滑动平均）。

        Args:
            observed_chars: 该次请求消息与工具定义的字符数（与估算口径一致）。
            prompt_tokens: 模型返回的 usage.prompt_tokens。

        Returns:
            None。
        """
        if observed_chars <= 0 or prompt_tokens <= 0:
            return
        observed_ratio = observed_chars / prompt_tokens
        with self._lock:
            self.chars_per_token = (
                1 - self.calibration_weight
            ) * self.chars_per_token + self.calibration_weight * observed_ratio


class TokenCalibrationHook:
    """模型响应后按 usage.prompt_tokens 校准 TokenEstimator。"""

    def __init__(self, estimator: TokenEstimator) -> None:
        """初始化校准 Hook。

        Args:
            estimator: 被校准的 token 估算器。

        Returns:
            None。
        """
        self.estimator = estimator  # 被校准的估算器。

    def register(self, hook_engine: HookEngine) -> None:
        """注册到模型响应事件；失败不影响主流程。

        Args:
            hook_engine: AgentLoop 使用的 HookEngine。

        Returns:
            None。
        """
        hook_engine.register(HookEvent.AFTER_MODEL_RESPONSE, self, critical=False)

    def __call__(self, context: HookContext) -> None:
        """读取本次请求的消息、工具定义与 usage 并校准。

        Args:
            context: 模型响应后的生命周期上下文。

        Returns:
            None。
        """
        result = context.result
        usage = result.get("usage") if isinstance(result, Mapping) else None
        prompt_tokens = (
            usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
        )
        if not isinstance(prompt_tokens, int):
            return
        messages = context.payload.get("messages")
        options = context.payload.get("request_options")
        tools = options.get("tools") if isinstance(options, Mapping) else None
        observed_chars = sum(
            self.estimator.measure_chars(dict(message))
            for message in messages or []
            if isinstance(message, Mapping)
        ) + (self.estimator.measure_chars(tools) if tools else 0)
        self.estimator.calibrate(observed_chars, prompt_tokens)
