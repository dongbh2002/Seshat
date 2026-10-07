"""记忆排序与衰减：按支持数与距最近一次被支持的时间计算得分，选取注入上下文的条目与交给记忆 agent 的现有条目。

    得分 = max(0, 支持分 − 反例分) × 0.5 ^ (距最近一次获得新支持依据的天数 / 半衰期)
文档级只在处理对应文档时使用、通用级经人工批准，二者不衰减，得分即净支持分。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from backend.config import MemorySettings
from backend.memory.models import MemoryItem
from backend.utils.text import containment

_RELEVANCE_NGRAM = 2  # 计算相关度时的字符片段长度。
_SECONDS_PER_DAY = 86400  # 一天的秒数。


class MemoryScorer:
    """计算记忆得分并按得分、新近程度与相关度选取条目。"""

    def __init__(self, settings: MemorySettings) -> None:
        """初始化记忆评分。

        Args:
            settings: 记忆配置，提供各级半衰期、休眠阈值与新条目名额。

        Returns:
            None。
        """
        self.settings = settings  # 记忆配置。
        self.half_life_days = {  # 会衰减的级别到半衰期天数。
            "user": settings.user_half_life_days,
            "tenant": settings.tenant_half_life_days,
        }

    def score(self, item: MemoryItem, now: datetime) -> float:
        """计算条目得分。

        Args:
            item: 记忆条目。
            now: 当前时间（带时区）。

        Returns:
            净支持分（支持分减反例分，不低于 0）衰减后的得分；不衰减的级别为净支持分。
        """
        net = max(0, item.support - item.against)
        half_life = self.half_life_days.get(item.level)
        if half_life is None:
            return float(net)
        elapsed = now - datetime.fromisoformat(item.last_supported_at)
        days = max(0.0, elapsed.total_seconds() / _SECONDS_PER_DAY)
        return net * 0.5 ** (days / half_life)

    def is_dormant(self, item: MemoryItem, now: datetime) -> bool:
        """判断条目是否应当休眠。

        Args:
            item: 记忆条目。
            now: 当前时间（带时区）。

        Returns:
            会衰减的级别且得分低于休眠阈值时为 True。
        """
        return (
            item.level in self.half_life_days
            and self.score(item, now) < self.settings.dormant_score
        )

    def select_for_context(
        self,
        items: Sequence[MemoryItem],
        limit: int,
        now: datetime,
    ) -> list[MemoryItem]:
        """选取注入上下文的生效条目：按得分取前列，并为最近新增的条目保留名额。

        Args:
            items: 归属下的条目（任意状态）。
            limit: 条数上限。
            now: 当前时间（带时区）。

        Returns:
            先按得分、再按新近程度排列的条目。
        """
        live = [
            item
            for item in items
            if item.status == "active" and not self.is_dormant(item, now)
        ]
        ranked = sorted(live, key=lambda item: self.score(item, now), reverse=True)
        chosen = ranked[: limit - self.settings.fresh_slots]
        chosen_ids = {item.id for item in chosen}
        rest = [item for item in ranked if item.id not in chosen_ids]
        rest.sort(key=lambda item: item.created_at, reverse=True)
        return chosen + rest[: limit - len(chosen)]

    def select_for_agent(
        self,
        items: Sequence[MemoryItem],
        query: str,
        limit: int,
        now: datetime,
    ) -> list[MemoryItem]:
        """选取交给记忆 agent 的现有条目：与本轮内容最相关的一半，加得分最高的一半。

        休眠条目也可入选，记忆 agent 强化后即恢复生效。

        Args:
            items: 归属下的生效或休眠条目。
            query: 本轮对话与信号的文本，用于计算相关度。
            limit: 条数上限。
            now: 当前时间（带时区）。

        Returns:
            去重后的条目，相关度入选的在前。
        """
        by_relevance = sorted(
            items,
            key=lambda item: containment(item.content, query, _RELEVANCE_NGRAM),
            reverse=True,
        )
        by_score = sorted(items, key=lambda item: self.score(item, now), reverse=True)
        chosen = by_relevance[: (limit + 1) // 2]
        chosen_ids = {item.id for item in chosen}
        chosen.extend(item for item in by_score if item.id not in chosen_ids)
        return chosen[:limit]
