"""记忆 agent 的编排：把一轮对话与本轮新信号交给记忆 agent，由它决定是否写入；校验后更新文档、用户、课题组（导师要求）三级记忆。

信号按字节位置消费，进度文件 <memory_root>/<tenant_id>/<user_id>/state.json：
{"schema_version": 1, "signal_position": 已处理到的信号文件字节位置}；某次调用失败时进度不前进，信号在下一轮重新处理。

代码层面的写入约束：
    每个操作须有证据；
    课题组级只记导师要求：新增或修改课题组级条目须有导师信号作证据，否则新增降为用户级、修改被忽略；
    用户级、课题组级内容与证据原文连续相同达到 privacy_min_run_chars 字时拒绝（防止论文原文进入共享记忆）；
    新增内容与同一归属下的生效或休眠条目完全相同时改为强化。

计分：每条依据按来源角色计分（evidence_weights，本轮用户表述按学生计），记录在条目上，同一依据只计一次；
同一轮中一条记忆新计入的支持分不超过 turn_support_cap（超出部分的依据记 0 分）；反例依据同样加权、去重，不封顶。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from backend.config import MemorySettings
from backend.llm_tasks import MemoryExtractor
from backend.memory.models import (
    LIVE_STATUSES,
    MEMORY_KINDS,
    SYSTEM_AUTHOR,
    MemoryItem,
    MemoryScope,
    find_live,
)
from backend.memory.scoring import MemoryScorer
from backend.memory.store import MemoryStore
from backend.signals import (
    SIGNAL_ROLES,
    DocumentLineage,
    Signal,
    SignalStore,
    TurnRecord,
)
from backend.utils.text import longest_common_run

STATE_FILENAME = "state.json"  # 用户记忆目录下的进度文件名。
RECORDED_LEVELS = ("document", "user", "tenant")  # 记忆 agent 可写入的级别。
SHARED_LEVELS = ("user", "tenant")  # 写入前须做原文泄露检查的级别。
TURN_EVIDENCE = "T"  # 证据编号：本轮对话中用户的表述。
TURN_ROLE = "student"  # 本轮对话中用户表述按此角色计分。
_SCHEMA_VERSION = 1  # 进度文件格式版本。
_SIGNAL_OVERHEAD_CHARS = 100  # 估算单条信号展示长度时附加的标签字符数。
_LOGGER = logging.getLogger(__name__)


class MemoryRecorder:
    """每轮结束后调用记忆 agent，把它决定写入的内容校验后写入记忆。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        scorer: MemoryScorer,
        signals: SignalStore,
        lineage: DocumentLineage,
        extractor: MemoryExtractor,
        settings: MemorySettings,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """初始化记忆 agent 编排。

        Args:
            store: 记忆存储。
            scorer: 记忆评分，用于挑选交给记忆 agent 的现有条目。
            signals: 当前用户的信号存储。
            lineage: 版本链，用于由信号的 revision 找到所属文档。
            extractor: 记忆 agent 的 LLM 调用。
            settings: 记忆配置。
            tenant_id: 当前课题组 ID。
            user_id: 当前用户 ID。

        Returns:
            None。

        Raises:
            ValueError: evidence_weights 缺少某个信号角色的分值。
        """
        missing = set(SIGNAL_ROLES) - set(settings.evidence_weights)
        if missing:
            raise ValueError(f"memory.evidence_weights 缺少角色: {sorted(missing)}")
        self.store = store  # 记忆存储。
        self.scorer = scorer  # 记忆评分。
        self.signals = signals  # 当前用户的信号存储。
        self.lineage = lineage  # 版本链。
        self.extractor = extractor  # 记忆 agent 的 LLM 调用。
        self.settings = settings  # 记忆配置。
        self.tenant_id = tenant_id  # 当前课题组 ID。
        self.user_id = user_id  # 当前用户 ID。
        self.state_path = (  # 信号处理进度文件。
            store.memory_root / tenant_id / user_id / STATE_FILENAME
        )

    def record(
        self,
        turn: TurnRecord | None,
        papers: Sequence[tuple[str, str]],
    ) -> set[str]:
        """处理一轮对话与尚未处理的信号；没有对话也没有新信号时不调用模型。

        Args:
            turn: 本轮对话；会话结束时处理遗留信号传 None。
            papers: 本会话涉及且来源已确认的文档 (文档 ID, 标题)，可写入文档级记忆。

        Returns:
            有条目被写入或修改的级别。

        Raises:
            ValueError: 模型返回无法解析的结果（已处理的批次不受影响）。
            OSError: 文件读写失败。
        """
        pending = self.signals.read_from(self._read_position())
        if turn is None and not pending:
            return set()
        changed: set[str] = set()
        gained: dict[str, int] = {}  # 本轮各条目已增加的支持分，跨批次累计以封顶。
        for batch in self._batches(pending):
            changed |= self._record_batch(
                turn, [signal for signal, _ in batch], papers, gained
            )
            if batch:
                self._write_position(batch[-1][1])
        return changed

    def _batches(
        self,
        entries: Sequence[tuple[Signal, int]],
    ) -> list[list[tuple[Signal, int]]]:
        """按估算的展示长度把信号分批；没有信号时返回一个空批，只处理对话。

        Args:
            entries: 待处理的 (信号, 该行结束后的字节位置)。

        Returns:
            每批总长度不超过 extract_max_chars 的分批（单条超长时独占一批）。
        """
        limit = self.settings.extract_max_chars
        excerpt = self.settings.signal_excerpt_chars
        batches: list[list[tuple[Signal, int]]] = [[]]
        size = 0
        for entry in entries:
            cost = _SIGNAL_OVERHEAD_CHARS + sum(
                min(len(text), excerpt) for text in _signal_texts(entry[0])
            )
            if batches[-1] and size + cost > limit:
                batches.append([])
                size = 0
            batches[-1].append(entry)
            size += cost
        return batches

    def _record_batch(
        self,
        turn: TurnRecord | None,
        batch: Sequence[Signal],
        papers: Sequence[tuple[str, str]],
        gained: dict[str, int],
    ) -> set[str]:
        """把一批信号连同本轮对话交给记忆 agent，并写入它决定的变更。

        Args:
            turn: 本轮对话；没有时为 None。
            batch: 一批信号，可以为空。
            papers: 本会话涉及的文档 (文档 ID, 标题)。
            gained: 本轮各条目已增加的支持分（原地累加）。

        Returns:
            有条目被写入或修改的级别。
        """
        evidence_ids = {
            f"S{index}": signal.id for index, signal in enumerate(batch, start=1)
        }
        source_texts = {signal.id: _signal_texts(signal) for signal in batch}
        roles = {signal.id: signal.role for signal in batch}
        if turn is not None:
            evidence_ids[TURN_EVIDENCE] = f"turn:{turn.run_id}"
            source_texts[evidence_ids[TURN_EVIDENCE]] = [turn.user_input]
            roles[evidence_ids[TURN_EVIDENCE]] = TURN_ROLE
        weights = {  # 依据 ID 到分值。
            evidence_id: self.settings.evidence_weights[role]
            for evidence_id, role in roles.items()
        }

        titles = dict(papers)  # 文档 ID 到标题。
        signal_papers: dict[str, str] = {}  # 信号 ID 到所属文档 ID。
        for signal in batch:
            record = self.lineage.get(signal.revision) if signal.revision else None
            if record is not None and record.paper_id:
                signal_papers[signal.id] = record.paper_id
                titles.setdefault(record.paper_id, record.title)
        paper_labels = {
            paper_id: f"P{index}" for index, paper_id in enumerate(titles, start=1)
        }
        paper_scopes = {
            label: MemoryScope("document", self.tenant_id, self.user_id, paper_id)
            for paper_id, label in paper_labels.items()
        }
        scopes = {
            "user": MemoryScope("user", self.tenant_id, self.user_id),
            "tenant": MemoryScope("tenant", self.tenant_id),
        }
        query_texts = [text for texts in source_texts.values() for text in texts]
        if turn is not None:
            query_texts.append(turn.reply)  # 用户输入已在 source_texts 中。
        query = " ".join(query_texts)
        existing = self._existing(
            [("", scopes["user"]), ("", scopes["tenant"]), *paper_scopes.items()],
            query,
        )

        operations = self.extractor.extract(
            dialogue=self._dialogue_view(turn),
            signals=[
                {
                    **signal.to_dict(),
                    "label": label,
                    "paper": paper_labels.get(signal_papers.get(signal.id, ""), ""),
                }
                for label, signal in zip(evidence_ids, batch)
            ],
            papers=[
                {"label": label, "title": titles[paper_id]}
                for paper_id, label in paper_labels.items()
            ],
            existing=[
                {
                    "id": item.id,
                    "level": item.level,
                    "paper": paper,
                    "kind": item.kind,
                    "dormant": item.status == "dormant",
                    "content": item.content,
                }
                for _, item, paper in existing.values()
            ],
            item_max_chars=self.settings.item_max_chars,
            excerpt_chars=self.settings.signal_excerpt_chars,
            dialogue_chars=self.settings.dialogue_max_chars,
        )

        changes: dict[MemoryScope, list[dict[str, Any]]] = {}
        for operation in operations:
            evidence = list(
                dict.fromkeys(
                    evidence_ids[label]
                    for label in operation.get("evidence", [])
                    if isinstance(label, str) and label in evidence_ids
                )
            )
            change = (
                self._validate(
                    operation,
                    evidence,
                    advised=any(roles[item] == "advisor" for item in evidence),
                    texts=[text for item in evidence for text in source_texts[item]],
                    scopes=scopes,
                    paper_scopes=paper_scopes,
                    existing=existing,
                )
                if evidence
                else None
            )
            if change is None:
                _LOGGER.warning("记忆操作无效、缺少证据或含原文，已忽略: %s", operation)
                continue
            scope, applied = change
            changes.setdefault(scope, []).append(applied)

        for scope, applied in changes.items():
            self.store.update(
                scope,
                lambda items, applied=applied: _apply(
                    items,
                    applied,
                    weights=weights,
                    cap=self.settings.turn_support_cap,
                    gained=gained,
                    history_limit=self.settings.history_max_entries,
                ),
            )
        return {scope.level for scope in changes}

    def _existing(
        self,
        sources: Sequence[tuple[str, MemoryScope]],
        query: str,
    ) -> dict[str, tuple[MemoryScope, MemoryItem, str]]:
        """为每个归属挑选交给记忆 agent 的生效或休眠条目。

        Args:
            sources: (文档编号, 归属)；非文档级的文档编号为空。
            query: 本轮对话与信号的文本。

        Returns:
            条目 ID 到 (归属, 条目, 文档编号)。
        """
        now = datetime.now().astimezone()
        existing: dict[str, tuple[MemoryScope, MemoryItem, str]] = {}
        for paper, scope in sources:
            live = [
                item for item in self.store.load(scope) if item.status in LIVE_STATUSES
            ]
            for item in self.scorer.select_for_agent(
                live, query, self.settings.agent_existing_max_items, now
            ):
                existing[item.id] = (scope, item, paper)
        return existing

    def _dialogue_view(self, turn: TurnRecord | None) -> dict[str, Any] | None:
        """把轮次记录整理为模板使用的对话视图。

        Args:
            turn: 轮次记录。

        Returns:
            user_input、reply、tool_calls（name、arguments 的 JSON 文本）；没有轮次时为 None。
        """
        if turn is None:
            return None
        return {
            "user_input": turn.user_input,
            "reply": turn.reply,
            "tool_calls": [
                {
                    "name": call.get("name"),
                    "arguments": json.dumps(call.get("arguments"), ensure_ascii=False),
                }
                for call in turn.tool_calls
            ],
        }

    def _validate(
        self,
        operation: Mapping[str, Any],
        evidence: list[str],
        *,
        advised: bool,
        texts: Sequence[str],
        scopes: Mapping[str, MemoryScope],
        paper_scopes: Mapping[str, MemoryScope],
        existing: Mapping[str, tuple[MemoryScope, MemoryItem, str]],
    ) -> tuple[MemoryScope, dict[str, Any]] | None:
        """校验记忆 agent 返回的一个操作，转为可执行的变更，约束见模块说明。

        Args:
            operation: 记忆 agent 返回的操作。
            evidence: 已映射为信号 ID 或轮次 ID 的证据。
            advised: 证据中是否有导师的信号。
            texts: 证据对应的原文（信号前后文本、批注、用户输入）。
            scopes: 用户级与课题组级归属。
            paper_scopes: 文档编号到文档级归属。
            existing: 交给记忆 agent 的现有条目 ID 到 (归属, 条目, 文档编号)。

        Returns:
            (归属, 变更)；操作无效时为 None。变更含 op、evidence（依据 ID），以及
            item（add，尚未计入依据）、id（support/revise/oppose）、content（revise）。
        """
        op = operation.get("op")
        content = operation.get("content")
        limit = self.settings.item_max_chars
        if op == "add":
            level = operation.get("level")
            kind = operation.get("kind")
            if level not in RECORDED_LEVELS or kind not in MEMORY_KINDS:
                return None
            if not isinstance(content, str) or not content.strip():
                return None
            if level == "tenant" and not advised:
                level = "user"
            if level in SHARED_LEVELS and self._leaks(content, texts):
                return None
            if level == "document":
                scope = paper_scopes.get(str(operation.get("paper")))
                if scope is None:
                    return None
            else:
                scope = scopes[level]
            item = MemoryItem.create(
                level=level,
                kind=kind,
                content=content[:limit],
                status="active",
                source="agent",
                created_by=SYSTEM_AUTHOR,
                evidence={},  # 写入时按来源加权并封顶后计入。
            )
            return scope, {"op": "add", "item": item, "evidence": evidence}
        if op in {"support", "revise", "oppose"}:
            target = existing.get(str(operation.get("id")))
            if target is None or (target[0].level == "tenant" and not advised):
                return None
            if op == "revise":
                if not isinstance(content, str) or not content.strip():
                    return None
                if target[0].level in SHARED_LEVELS and self._leaks(content, texts):
                    return None
            return target[0], {
                "op": op,
                "id": target[1].id,
                "evidence": evidence,
                "content": content[:limit] if op == "revise" else "",
            }
        return None

    def _leaks(self, content: str, texts: Sequence[str]) -> bool:
        """判断记忆内容是否照抄了证据原文。

        Args:
            content: 记忆内容。
            texts: 证据原文。

        Returns:
            与任一原文连续相同达到 privacy_min_run_chars 字时为 True。
        """
        threshold = self.settings.privacy_min_run_chars
        return any(longest_common_run(content, text) >= threshold for text in texts)

    def _read_position(self) -> int:
        """读取已处理到的信号文件字节位置。

        Returns:
            进度文件中的位置；文件不存在时为 0。

        Raises:
            ValueError: 文件格式版本不符。
        """
        if not self.state_path.is_file():
            return 0
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(f"记忆进度文件格式版本不符: {self.state_path}")
        return int(data["signal_position"])

    def _write_position(self, position: int) -> None:
        """原子写入已处理到的信号文件字节位置。

        Args:
            position: 字节位置。

        Returns:
            None。

        Raises:
            OSError: 文件写入失败。
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"schema_version": _SCHEMA_VERSION, "signal_position": position}
            ),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, self.state_path)


def _signal_texts(signal: Signal) -> list[str]:
    """取信号中的原文：修改前后、批注或决定、Seshat 建议。

    Args:
        signal: 修改信号。

    Returns:
        非空文本列表。
    """
    parts = (signal.before, signal.after, signal.comment, signal.suggestion)
    return [part for part in parts if part]


def _apply(
    items: list[MemoryItem],
    changes: Sequence[Mapping[str, Any]],
    *,
    weights: Mapping[str, int],
    cap: int,
    gained: dict[str, int],
    history_limit: int,
) -> None:
    """把一个归属下的变更应用到条目列表：依据按来源计分、同一依据只计一次、支持分按轮封顶。

    Args:
        items: 归属下的条目（原地修改）。
        changes: ``MemoryRecorder._validate`` 生成的变更。
        weights: 依据 ID 到分值。
        cap: 同一轮中一条记忆最多新计入的支持分。
        gained: 本轮各条目已新计入的支持分（原地累加）。
        history_limit: 修改历史保留条数。

    Returns:
        None。
    """

    def allocate(item: MemoryItem, evidence: Sequence[str]) -> dict[str, int]:
        """为条目尚未记录的依据分配计入分数，受本轮剩余额度限制（用尽后记 0 分）。

        Args:
            item: 记忆条目。
            evidence: 本次的依据 ID。

        Returns:
            新依据 ID 到计入的分。
        """
        remaining = cap - gained.get(item.id, 0)
        credits: dict[str, int] = {}
        for evidence_id in evidence:
            if evidence_id in item.evidence or evidence_id in credits:
                continue
            credits[evidence_id] = max(0, min(weights[evidence_id], remaining))
            remaining -= credits[evidence_id]
        gained[item.id] = gained.get(item.id, 0) + sum(credits.values())
        return credits

    by_id = {item.id: item for item in items}
    for change in changes:
        if change["op"] == "add":
            target = find_live(items, change["item"].content)
            if target is None:
                target = change["item"]
                items.append(target)
                by_id[target.id] = target
            target.credit(allocate(target, change["evidence"]))
            continue
        item = by_id.get(change["id"])
        if item is None or item.status not in LIVE_STATUSES:
            continue
        if change["op"] == "oppose":
            item.debit(
                {
                    evidence_id: weights[evidence_id]
                    for evidence_id in change["evidence"]
                },
                history_limit=history_limit,
            )
            continue
        if change["op"] == "revise":
            item.change_content(
                change["content"], reason="记忆 agent 修订", history_limit=history_limit
            )
        item.credit(allocate(item, change["evidence"]))
