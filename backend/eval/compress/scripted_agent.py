"""脚本化模型：替代真实 LLM，在真实 Runtime 上按固定剧本发起工具调用，保证评估可复现。

模型只根据“这一步请求里实际能看到的内容”做决定：所需的块不在上下文中就重读，
看得到（哪怕是过时版本）就直接使用——与真实模型一样无法分辨过时正文。
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count
from types import SimpleNamespace
from typing import Any

from openai.types.chat import ChatCompletion

from backend.eval.compress.metrics import DocumentTruth, request_chars, visible_blocks
from backend.utils.docx import DocumentIndex, DocumentSection

Messages = Sequence[Mapping[str, Any]]
_COMPLIANT = "compliant"  # 会记录审阅进度、问题与用户约束的行为。
_NONCOMPLIANT = "noncompliant"  # 从不调用 update_review_state 的行为。
_CONSTRAINT = "之后修改时请不要改动引用格式，所有修改用修订模式。"  # 用户约束原话。
CONSTRAINT_KEY = "不要改动引用格式"  # 判断约束是否仍在上下文中的关键词。
_EDIT_SUFFIX = "（已润色）"  # 编辑时追加在首词后的文字。
_MAX_READS_PER_NEED = 4  # 单次补读最多调用次数，防止异常时死循环。
_MIN_PARAGRAPH_CHARS = 20  # 可作为编辑目标与问题定位的段落最少字符数。
_WORD = re.compile(r"[A-Za-z]+|[一-鿿]{1,8}")  # 编辑定位用的首个词。
_FILLER = "（模拟回复的补充说明，用于占据真实回复的长度。）"  # 回复填充文本。


@dataclass
class ModelAction:
    """脚本化模型的一次输出：工具调用或最终回复。"""

    tool_calls: list[dict[str, Any]] = field(
        default_factory=list
    )  # 含 id/name/arguments。
    content: str = ""  # 最终回复文本；有工具调用时为空。
    reread_ids: list[str] = field(
        default_factory=list
    )  # 因所需内容缺失而发起的读取调用 ID。


@dataclass
class TurnPlan:
    """一轮用户输入及其执行剧本。"""

    label: str  # 轮次标签，用于报告。
    user: str  # 用户输入原文。
    run: Callable[[Messages], Generator[ModelAction, Messages, None]]  # 执行剧本。
    needs: Callable[[], list[str]] = list  # 本轮回答需要的块 ID（轮次开始时求值）。
    recall_keys: list[str] = field(default_factory=list)  # 本轮检查的早先用户请求。


class ScriptedAgent:
    """按剧本逐轮行动的模型；剧本中的章节按文档结构通用地选出。

    文档结构与当前版本只用于“出题”（本轮需要哪些块、编辑哪一段）；
    是否重读、用哪段正文、用哪个 revision，只看请求中的上下文。
    """

    def __init__(
        self,
        document_index: DocumentIndex,
        truth: DocumentTruth,
        path: str,
        behavior: str,
        answer_chars: int,
    ) -> None:
        """根据文档结构生成剧本。

        Args:
            document_index: 评估文档所在目录的结构索引。
            truth: 评估文档的当前版本（出题用）。
            path: 文档相对路径。
            behavior: ``compliant`` 或 ``noncompliant``。
            answer_chars: 每次最终回复的字符数。

        Returns:
            None。

        Raises:
            ValueError: 行为未知，或文档可用章节不足 4 个。
        """
        if behavior not in {_COMPLIANT, _NONCOMPLIANT}:
            raise ValueError(f"未知行为: {behavior}")
        self.index = document_index  # 文档结构索引。
        self.truth = truth  # 文档当前版本，出题用。
        self.path = path  # 文档相对路径。
        self.compliant = behavior == _COMPLIANT  # 是否记录审阅状态。
        self.answer_chars = answer_chars  # 回复字符数。
        self.turns = self._build_turns()  # 全部轮次剧本。
        self.constraint_turn = 1  # 用户提出约束的轮次下标（剧本第 2 轮）。
        self.turn_index = -1  # 当前轮次下标。
        self.turn: TurnPlan | None = None  # 当前轮次剧本。
        self.turn_needs: list[str] = []  # 当前轮次需要的块 ID。
        self.turn_start = False  # 下一次请求是否是本轮第一次请求。
        self.edited_block: str | None = None  # 编辑轮次修改的块 ID。
        self._plan: Generator[ModelAction, Messages, None] | None = None  # 剧本生成器。
        self._call_ids = count(1)  # 全局唯一的工具调用编号。
        self._issued_reads: set[str] = set()  # 已发起、待登记审阅的读取调用 ID。
        self._finding_blocks: set[str] = set()  # 已记录过问题的块，避免重复。
        self._queued_answer: ModelAction | None = None  # 先记录审阅、下一步再给的回复。

    # ---- 轮次驱动 ----

    def start_turn(self, index: int) -> str:
        """开始第 index 轮，返回用户输入。

        Args:
            index: 轮次下标。

        Returns:
            该轮用户输入。
        """
        self.turn_index = index
        self.turn = self.turns[index]
        self.turn_needs = self.turn.needs()
        self.turn_start = True
        self._plan = None
        self._queued_answer = None
        return self.turn.user

    def act(self, messages: Messages) -> ModelAction:
        """根据本次请求的消息给出下一步动作；合规行为会先登记新读到的正文。

        Args:
            messages: 本次请求的全部消息（即模型实际看到的上下文）。

        Returns:
            工具调用或最终回复。
        """
        self.turn_start = False
        if self._queued_answer is not None:
            action, self._queued_answer = self._queued_answer, None
            return action
        records = self._pending_records(messages) if self.compliant else []
        if self._plan is None:
            assert self.turn is not None
            self._plan = self.turn.run(messages)
            action = next(self._plan)
        else:
            action = self._plan.send(messages)
        if not records:
            return action
        record_call = self._call("update_review_state", {"operations": records})
        if action.tool_calls:
            action.tool_calls.insert(0, record_call)
            return action
        self._queued_answer = action
        return ModelAction(tool_calls=[record_call])

    # ---- 剧本 ----

    def _build_turns(self) -> list[TurnPlan]:
        """按章节大小与位置选出评估用章节，生成 10 轮剧本。

        最大的三章用于压力较大的阅读；位置最靠前的其余章节用于编辑，
        之后再回到它，考察修改后的正文能否正确找回。

        Returns:
            轮次剧本列表。

        Raises:
            ValueError: 含正文段落的章节不足 4 个。
        """
        snapshot = self.index.load(self.path)
        sections = [s for s in snapshot.sections if s.title and self._paragraphs(s)]
        if len(sections) < 4:
            raise ValueError("评估文档至少需要 4 个含正文段落的章节")
        big, second, third = sorted(sections, key=lambda s: -len(s.markdown))[:3]
        edit = next(s for s in sections if s not in {big, second, third})
        target = self._paragraphs(edit)[0]
        users = [
            f"请通读 {self.path}，概括全文结构和主要问题。",
            _CONSTRAINT,
            f"「{edit.title}」这一章主要讲了什么？有什么问题？",
            f"把「{edit.title}」这一章第一段的开头润色一下。",
            f"「{second.title}」这一章写得怎么样？",
            "刚才改的那一段现在是什么样的？",
            f"检查一下「{third.title}」这一章，注意遵守我之前提的修改要求。",
            f"详细审阅「{big.title}」这一章。",
            f"回到「{edit.title}」，再看看还有什么问题。",
            "总结一下这次对话我们做了什么、我提过哪些要求。",
        ]
        plans = [
            TurnPlan("T1 通读", users[0], self._read_all),
            TurnPlan("T2 约束", users[1], self._record_constraint),
            TurnPlan("T3 问章节", users[2], self._review(edit), self._needs(edit)),
            TurnPlan("T4 编辑", users[3], self._edit(target), lambda: [target]),
            TurnPlan("T5 问章节", users[4], self._review(second), self._needs(second)),
            TurnPlan(
                "T6 查改动", users[5], self._review_blocks([target]), lambda: [target]
            ),
            TurnPlan("T7 查+约束", users[6], self._review(third), self._needs(third)),
            TurnPlan("T8 大章节", users[7], self._review(big), self._needs(big)),
            TurnPlan("T9 回到旧章", users[8], self._review(edit), self._needs(edit)),
            TurnPlan("T10 回顾", users[9], self._recap, recall_keys=users[:9]),
        ]
        return plans

    def _read_all(self, messages: Messages) -> Generator[ModelAction, Messages, None]:
        """先读大纲，再按续读 ID 分页读完全文，最后概括。

        Args:
            messages: 本轮第一次请求的消息。

        Yields:
            工具调用与最终回复。
        """
        call = self._call("read_document", {"path": self.path, "view": "outline"})
        messages = yield ModelAction(tool_calls=[call])
        arguments: dict[str, Any] = {"path": self.path}
        while True:
            call = self._call("read_document", arguments, content_read=True)
            messages = yield ModelAction(tool_calls=[call])
            next_id = (_tool_results(messages).get(call["id"]) or {}).get("next_id")
            if not next_id:
                break
            arguments = {"path": self.path, "start_id": next_id}
        yield self._answer("全文结构与主要问题概括如下。")

    def _record_constraint(
        self,
        messages: Messages,
    ) -> Generator[ModelAction, Messages, None]:
        """合规行为把用户约束记为决定；否则只口头答应。

        Args:
            messages: 本轮第一次请求的消息。

        Yields:
            工具调用与最终回复。
        """
        if self.compliant:
            operation = {"op": "add_decision", "text": _CONSTRAINT}
            yield ModelAction(
                tool_calls=[
                    self._call("update_review_state", {"operations": [operation]})
                ]
            )
        yield self._answer("好的，之后修改会保留引用格式并使用修订模式。")

    def _review(
        self,
        section: DocumentSection,
    ) -> Callable[[Messages], Generator[ModelAction, Messages, None]]:
        """生成“审阅某一章”的剧本。

        Args:
            section: 要审阅的章节。

        Returns:
            剧本函数。
        """
        return self._review_blocks(list(section.block_ids))

    def _review_blocks(
        self,
        block_ids: list[str],
    ) -> Callable[[Messages], Generator[ModelAction, Messages, None]]:
        """生成“基于若干块作答”的剧本：缺什么补读什么，再回答。

        Args:
            block_ids: 作答需要的块 ID。

        Returns:
            剧本函数。
        """

        def run(messages: Messages) -> Generator[ModelAction, Messages, None]:
            """补读缺失的块后作答。

            Args:
                messages: 本轮第一次请求的消息。

            Yields:
                工具调用与最终回复。
            """
            needed = self._content_ids(block_ids)
            yield from self._ensure_visible(messages, needed)
            yield self._answer(f"基于 {len(needed)} 个块的审阅意见如下。")

        return run

    def _edit(
        self,
        target: str,
    ) -> Callable[[Messages], Generator[ModelAction, Messages, None]]:
        """生成编辑剧本：按上下文中看到的正文修改目标块首词，失败则重读后重试，最后复核。

        Args:
            target: 被编辑的块 ID。

        Returns:
            剧本函数。
        """

        def run(messages: Messages) -> Generator[ModelAction, Messages, None]:
            """执行一次修订式替换并复核。

            Args:
                messages: 本轮第一次请求的消息。

            Yields:
                工具调用与最终回复。
            """
            messages = yield from self._ensure_visible(messages, [target])
            for attempt in range(2):
                word = _first_word(_latest_text(messages, target))
                revision = self._latest_revision(messages)
                if word is None or revision is None or attempt == 1:
                    messages = yield from self._read_range(target, target, reread=True)
                    word = _first_word(_latest_text(messages, target))
                    revision = self._latest_revision(messages)
                if word is None or revision is None:
                    break
                operation = {
                    "op": "replace_text",
                    "id": target,
                    "old": word,
                    "new": f"{word}{_EDIT_SUFFIX}",
                }
                call = self._call(
                    "write_document",
                    {
                        "path": self.path,
                        "revision": revision,
                        "operations": [operation],
                        "mode": "tracked",
                    },
                )
                messages = yield ModelAction(tool_calls=[call])
                if "error" not in (_tool_results(messages).get(call["id"]) or {}):
                    self.edited_block = target
                    break
            yield from self._read_range(target, target, reread=False)
            yield self._answer("已完成修改并复核。")

        return run

    def _recap(self, messages: Messages) -> Generator[ModelAction, Messages, None]:
        """只凭上下文回顾整个对话。

        Args:
            messages: 本轮第一次请求的消息。

        Yields:
            最终回复。
        """
        yield self._answer("本次对话回顾如下。")

    # ---- 读取与登记 ----

    def _ensure_visible(
        self,
        messages: Messages,
        block_ids: list[str],
    ) -> Generator[ModelAction, Messages, Messages]:
        """所需块不在上下文中时，读取覆盖缺失块的最小连续范围，直到都可见。

        Args:
            messages: 当前请求的消息。
            block_ids: 所需块 ID（按文档顺序）。

        Yields:
            读取调用。

        Returns:
            最新一次请求的消息。
        """
        for _ in range(_MAX_READS_PER_NEED):
            seen = {block_id for block_id, _ in visible_blocks(messages)}
            missing = [block_id for block_id in block_ids if block_id not in seen]
            if not missing:
                break
            messages = yield from self._read_range(missing[0], missing[-1], reread=True)
        return messages

    def _read_range(
        self,
        start_id: str,
        end_id: str,
        *,
        reread: bool,
    ) -> Generator[ModelAction, Messages, Messages]:
        """读取闭区间内的块，范围超出单次上限时按续读 ID 继续。

        Args:
            start_id: 起始块 ID。
            end_id: 结束块 ID。
            reread: 是否因内容缺失而重读（计入重读成本）。

        Yields:
            读取调用。

        Returns:
            最新一次请求的消息。
        """
        arguments = {"path": self.path, "start_id": start_id, "end_id": end_id}
        while True:
            call = self._call("read_document", arguments, content_read=True)
            messages = yield ModelAction(
                tool_calls=[call],
                reread_ids=[call["id"]] if reread else [],
            )
            next_id = (_tool_results(messages).get(call["id"]) or {}).get("next_id")
            if not next_id:
                return messages
            arguments = {**arguments, "start_id": next_id}

    def _pending_records(self, messages: Messages) -> list[dict[str, Any]]:
        """为新返回的正文读取生成审阅登记：标记已审阅，并对首个段落记录一个问题。

        Args:
            messages: 当前请求的消息。

        Returns:
            update_review_state 的操作列表；没有新读取时为空。
        """
        results = _tool_results(messages)
        operations: list[dict[str, Any]] = []
        for call_id in sorted(self._issued_reads & results.keys()):
            self._issued_reads.discard(call_id)
            index = [
                item
                for item in (results[call_id] or {}).get("index", [])
                if "id" in item
            ]
            if not index:
                continue
            operations.append(
                {
                    "op": "mark_reviewed",
                    "path": self.path,
                    "start_id": index[0]["id"],
                    "end_id": index[-1]["id"],
                }
            )
            paragraph = next(
                (
                    item["id"]
                    for item in index
                    if item.get("kind") == "paragraph"
                    and item["id"] not in self._finding_blocks
                ),
                None,
            )
            if paragraph is not None:
                self._finding_blocks.add(paragraph)
                operations.append(
                    {
                        "op": "add_finding",
                        "path": self.path,
                        "block_id": paragraph,
                        "issue": f"段落 {paragraph} 表述冗长，建议精简并补充依据。",
                    }
                )
        return operations

    def _latest_revision(self, messages: Messages) -> str | None:
        """从最新的工具结果中找到文档 revision。

        Args:
            messages: 当前请求的消息。

        Returns:
            revision；上下文中没有时为 None。
        """
        for result in reversed(list(_tool_results(messages).values())):
            if (
                result
                and result.get("path") == self.path
                and isinstance(result.get("revision"), str)
            ):
                return result["revision"]
        return None

    # ---- 小工具 ----

    def _paragraphs(self, section: DocumentSection) -> list[str]:
        """返回章节中足够长的正文段落 ID（适合作为编辑目标与问题定位）。

        Args:
            section: 章节。

        Returns:
            段落块 ID 列表。
        """
        blocks = [self.truth.blocks.get(block_id) for block_id in section.block_ids]
        return [
            block.block_id
            for block in blocks
            if block is not None
            and block.kind == "paragraph"
            and len(block.text.strip()) >= _MIN_PARAGRAPH_CHARS
        ]

    def _content_ids(self, block_ids: list[str]) -> list[str]:
        """去掉当前版本中正文为空的块（如修订删除后的空段落）。

        Args:
            block_ids: 候选块 ID。

        Returns:
            正文非空的块 ID。
        """
        self.truth.refresh()
        return self.truth.content_ids(block_ids)

    def _needs(self, section: DocumentSection) -> Callable[[], list[str]]:
        """返回“本章正文块”的延迟求值函数。

        Args:
            section: 章节。

        Returns:
            轮次开始时调用的函数。
        """
        return lambda: self._content_ids(list(section.block_ids))

    def _call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        content_read: bool = False,
    ) -> dict[str, Any]:
        """生成一次工具调用。

        Args:
            name: 工具名。
            arguments: 工具参数。
            content_read: 是否是正文读取（合规行为读完后需要登记）。

        Returns:
            含 id、name、arguments 的调用描述。
        """
        call_id = f"call_{next(self._call_ids)}"
        if content_read:
            self._issued_reads.add(call_id)
        return {"id": call_id, "name": name, "arguments": dict(arguments)}

    def _answer(self, text: str) -> ModelAction:
        """生成固定长度的最终回复。

        Args:
            text: 回复开头。

        Returns:
            最终回复动作。
        """
        assert self.turn is not None
        content = f"[{self.turn.label}] {text}"
        filler = _FILLER * math.ceil(self.answer_chars / len(_FILLER))
        return ModelAction(content=(content + filler)[: self.answer_chars])


class ScriptedModelClient:
    """鸭子类型的 OpenAI 客户端：``chat.completions.create`` 返回 SDK 同款 ChatCompletion。"""

    def __init__(
        self,
        agent: ScriptedAgent,
        chars_per_token: float,
        observer: Callable[[Messages, Sequence[Mapping[str, Any]]], None],
        action_observer: Callable[[ModelAction], None],
    ) -> None:
        """初始化脚本化客户端。

        Args:
            agent: 决定每一步动作的脚本化模型。
            chars_per_token: 计算 usage.prompt_tokens 的字符/token 比例。
            observer: 每次请求到达时回调（消息、工具定义），用于测量。
            action_observer: 每次给出动作后回调，用于统计重读。

        Returns:
            None。
        """
        self.agent = agent  # 脚本化模型。
        self.chars_per_token = chars_per_token  # usage 折算比例。
        self.observer = observer  # 请求测量回调。
        self.action_observer = action_observer  # 动作回调。
        self.chat = SimpleNamespace(  # 模拟 SDK 的 client.chat.completions 结构。
            completions=SimpleNamespace(create=self._create)
        )

    def _create(
        self,
        *,
        model: str,
        messages: Messages,
        tools: Sequence[Mapping[str, Any]] = (),
        **_: Any,
    ) -> ChatCompletion:
        """处理一次模型请求：测量上下文，交给脚本化模型决定动作，返回 ChatCompletion。

        Args:
            model: 模型名。
            messages: 请求消息。
            tools: 工具定义。
            **_: 其余请求参数（忽略）。

        Returns:
            与 OpenAI SDK 结构一致的响应。
        """
        self.observer(messages, tools)
        action = self.agent.act(messages)
        self.action_observer(action)
        tool_calls = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                },
            }
            for call in action.tool_calls
        ]
        prompt_tokens = math.ceil(request_chars(messages, tools) / self.chars_per_token)
        completion_tokens = math.ceil(
            len(action.content or json.dumps(tool_calls, ensure_ascii=False))
            / self.chars_per_token
        )
        return ChatCompletion.model_validate(
            {
                "id": f"scripted-{time.monotonic_ns()}",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if tool_calls else "stop",
                        "message": {
                            "role": "assistant",
                            "content": action.content or None,
                            "tool_calls": tool_calls or None,
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )


def _tool_results(messages: Messages) -> dict[str, dict[str, Any] | None]:
    """按调用 ID 解析上下文中的工具结果。

    Args:
        messages: 请求消息。

    Returns:
        调用 ID 到结果字典（非 JSON 对象时为 None），按出现顺序。
    """
    results: dict[str, dict[str, Any] | None] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            value = json.loads(str(message.get("content")))
        except json.JSONDecodeError:
            value = None
        results[str(message.get("tool_call_id"))] = (
            value if isinstance(value, dict) else None
        )
    return results


def _latest_text(messages: Messages, block_id: str) -> str | None:
    """返回上下文中某块最后一次出现的正文（去掉 ID 标记）。

    Args:
        messages: 请求消息。
        block_id: 块 ID。

    Returns:
        正文；不可见时为 None。
    """
    texts = [text for found, text in visible_blocks(messages) if found == block_id]
    return texts[-1].removeprefix(f"[{block_id}]").strip() if texts else None


def _first_word(text: str | None) -> str | None:
    """取正文中第一个英文单词或最多 8 个连续汉字，作为替换定位文字。

    Args:
        text: 块正文。

    Returns:
        定位文字；没有时为 None。
    """
    match = _WORD.search(text or "")
    return match.group(0) if match else None
