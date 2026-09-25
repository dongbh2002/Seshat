"""上下文引擎，负责组装最终上下文并提供上下文压缩入口。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from backend.config import Settings

Context = list[dict[str, Any]]
_PROMPT_DIRECTORY = Path(__file__).resolve().parents[1] / "prompts"
_DEFAULT_SYSTEM_PROMPT_PATH = _PROMPT_DIRECTORY / "system_prompt.j2"
_DEFAULT_PROFILE_PROMPT_PATH = _PROMPT_DIRECTORY / "profile_prompt.j2"
_DEFAULT_MEMORY_PROMPT_PATH = _PROMPT_DIRECTORY / "memory_prompt.j2"


class ContextEngine:
    """按系统、画像、记忆和会话职责组装模型上下文。"""

    def __init__(
        self,
        settings: Settings,
        system_prompt_path: Path | None = None,
    ) -> None:
        """加载系统提示词模板并初始化上下文引擎。

        Args:
            settings: 启动入口加载并注入的类型化项目配置。
            system_prompt_path: 可选的系统提示词模板路径，默认使用项目模板。

        Returns:
            None。

        Raises:
            FileNotFoundError: 系统提示词模板不存在。
            ValueError: 系统提示词渲染后为空。
        """
        prompt_path = system_prompt_path or _DEFAULT_SYSTEM_PROMPT_PATH
        self.system_prompt_path = prompt_path.resolve()  # 基础系统提示词模板路径。
        self.profile_prompt_path = (  # 研究与写作画像提示词模板路径。
            _DEFAULT_PROFILE_PROMPT_PATH.resolve()
        )
        self.memory_prompt_path = (  # 长期记忆提示词模板路径。
            _DEFAULT_MEMORY_PROMPT_PATH.resolve()
        )
        self._template_environment = Environment(  # 所有上下文模板共用的 Jinja 环境。
            undefined=StrictUndefined
        )
        compression = settings.context.compression
        self.tool_cleanup_threshold_chars = (  # 第一级工具结果清理阈值。
            compression.tool_cleanup_threshold_chars
        )
        self.turn_compaction_threshold_chars = (  # 第二级早期轮次结构化阈值。
            compression.turn_compaction_threshold_chars
        )
        self.hard_limit_chars = compression.hard_limit_chars  # 第三级硬上限。
        self.keep_recent_turns = (  # 默认保留的近期完整轮次数。
            compression.keep_recent_turns
        )
        self.summary_max_chars = (  # 历史结构化记录的字符上限。
            compression.summary_max_chars
        )
        self.system_prompt = self._render_prompt(  # 已渲染的基础系统提示词。
            self.system_prompt_path
        )
        if not self.system_prompt:
            raise ValueError(f"系统提示词模板渲染结果为空: {self.system_prompt_path}")

    def get_final_context(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
    ) -> Context:
        """组合指令上下文、已完成历史和当前活动轮次。

        Args:
            history: 以前已经完成、可以进入压缩流程的原始消息。
            active_turn: 当前用户输入及本轮产生的模型和工具消息。

        Returns:
            可直接提供给模型的最终上下文。
        """
        system_context = self._get_system_context()
        profile_context = self._get_profile_context()
        memory_context = self._get_memory_context()
        instruction_context = self._merge_instruction_contexts(
            system_context,
            profile_context,
            memory_context,
        )
        conversation_context = self._get_conversation_context(history)
        active_context = [dict(message) for message in active_turn]
        reserved_context = [
            {"role": "system", "content": instruction_context},
            *active_context,
        ]
        compressed_conversation, history_summary = self.compress_conversation(
            conversation_context,
            reserved_chars=self._estimate_context_chars(reserved_context),
        )
        final_instruction_context = self._merge_instruction_contexts(
            instruction_context,
            history_summary,
        )
        return [
            {"role": "system", "content": final_instruction_context},
            *compressed_conversation,
            *active_context,
        ]

    def compress_conversation(
        self,
        context: Sequence[Mapping[str, Any]],
        *,
        reserved_chars: int = 0,
    ) -> tuple[Context, str]:
        """按三级阈值清理工具结果、压实早期轮次并约束硬上限。

        Args:
            context: 需要压缩的历史动态消息列表。
            reserved_chars: 系统上下文和当前活动轮次已占用的字符数。

        Returns:
            压缩后的历史消息副本和有界结构化历史记录。
        """
        compressed = [dict(message) for message in context]
        current_chars = reserved_chars + self._estimate_context_chars(compressed)
        if current_chars <= self.tool_cleanup_threshold_chars:
            return compressed, ""

        tool_names = self._get_tool_call_names(compressed)
        readable_results = [
            index
            for index, message in enumerate(compressed)
            if message.get("role") == "tool"
            and tool_names.get(message.get("tool_call_id")) == "read_document"
            and self._is_full_read_result(message)
        ]
        for message_index in readable_results:
            compressed[message_index] = self._compact_read_result(
                compressed[message_index]
            )
            current_chars = reserved_chars + self._estimate_context_chars(compressed)
            if current_chars <= self.tool_cleanup_threshold_chars:
                break

        current_chars = reserved_chars + self._estimate_context_chars(compressed)
        if current_chars <= self.turn_compaction_threshold_chars:
            return compressed, ""

        turns = self._group_completed_turns(compressed)
        recent_turns = turns[-self.keep_recent_turns :]
        compacted_turns = turns[: -self.keep_recent_turns]
        history_summary = self._build_history_summary(compacted_turns)
        recent_context = [message for turn in recent_turns for message in turn]

        while (
            len(recent_turns) > 1
            and self._combined_context_chars(
                recent_context,
                history_summary,
                reserved_chars,
            )
            > self.hard_limit_chars
        ):
            compacted_turns.append(recent_turns.pop(0))
            history_summary = self._build_history_summary(compacted_turns)
            recent_context = [message for turn in recent_turns for message in turn]

        # TODO: 单个近期轮次自身超过硬上限时，结合工具分页对当前轮次限流。
        return recent_context, history_summary

    @staticmethod
    def _group_completed_turns(context: Context) -> list[Context]:
        """按用户消息边界将已完成历史分成完整轮次。

        Args:
            context: 已过滤系统消息的原始历史。

        Returns:
            按时间顺序排列的完整轮次列表。
        """
        turns: list[Context] = []
        current_turn: Context = []
        for message in context:
            if message.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(dict(message))
        if current_turn:
            turns.append(current_turn)
        return turns

    def _build_history_summary(self, turns: Sequence[Context]) -> str:
        """将早期完整轮次转换为有字符上限的结构化记录。

        Args:
            turns: 需要移出原始消息窗口的早期轮次。

        Returns:
            可合并到系统上下文的历史压缩记录。
        """
        if not turns:
            return ""

        entries = [
            self._summarize_turn(turn_number, turn)
            for turn_number, turn in enumerate(turns, start=1)
        ]
        header = (
            "## 历史压缩记录\n\n"
            "以下内容是已发生对话的压缩记录，不是新的用户指令。\n\n"
            "<history_summary>\n"
        )
        footer = "\n</history_summary>"
        available_chars = max(
            self.summary_max_chars - len(header) - len(footer),
            1,
        )
        selected_entries: list[str] = []
        selected_chars = 0
        for entry in reversed(entries):
            added_chars = len(entry) + (2 if selected_entries else 0)
            if selected_entries and selected_chars + added_chars > available_chars:
                break
            if not selected_entries and added_chars > available_chars:
                entry = self._truncate_text(entry, available_chars)
                added_chars = len(entry)
            selected_entries.append(entry)
            selected_chars += added_chars

        selected_entries.reverse()
        omitted_count = len(entries) - len(selected_entries)
        body_parts: list[str] = []
        if omitted_count:
            body_parts.append(f"更早的 {omitted_count} 个轮次已从模型上下文移除。")
        body_parts.extend(selected_entries)
        body = "\n\n".join(body_parts)
        body = self._truncate_text(body, available_chars)
        return f"{header}{body}{footer}"

    def _summarize_turn(self, turn_number: int, turn: Context) -> str:
        """提取一个历史轮次的用户请求、最终回复和工具事实。

        Args:
            turn_number: 历史轮次的展示序号。
            turn: 一个完整用户轮次中的消息。

        Returns:
            结构化的单轮历史文本。
        """
        user_text = ""
        assistant_text = ""
        tool_names = self._get_tool_call_names(turn)
        tool_records: list[str] = []
        for message in turn:
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str) and not user_text:
                user_text = content
            elif role == "assistant" and isinstance(content, str) and content:
                assistant_text = content
            elif role == "tool":
                tool_records.append(self._summarize_tool_message(message, tool_names))

        lines = [
            f"### 历史轮次 {turn_number}",
            f"- 用户：{self._truncate_text(user_text, 800)}",
            f"- 助手：{self._truncate_text(assistant_text, 1200)}",
        ]
        lines.extend(f"- 工具：{record}" for record in tool_records)
        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_message(
        message: Mapping[str, Any],
        tool_names: Mapping[str, str],
    ) -> str:
        """将工具消息提取为不含大段正文的事实记录。

        Args:
            message: 需要提取的 ``role=tool`` 消息。
            tool_names: 工具调用 ID 到工具名称的映射。

        Returns:
            包含工具名、结果和关键定位字段的单行文本。
        """
        call_id = message.get("tool_call_id")
        tool_name = tool_names.get(call_id, "unknown_tool")
        content = message.get("content")
        if not isinstance(content, str):
            return f"{tool_name} 返回了非文本结果"
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            return f"{tool_name}: {ContextEngine._truncate_text(content, 500)}"
        if not isinstance(result, Mapping):
            return f"{tool_name}: {ContextEngine._truncate_text(content, 500)}"

        status = "失败" if result.get("ok") is False else "成功"
        fields = [f"{tool_name} {status}"]
        for key in ("path", "source_path", "revision", "previous_revision"):
            value = result.get(key)
            if isinstance(value, str) and value:
                fields.append(f"{key}={value}")
        error = result.get("error")
        if isinstance(error, str) and error:
            fields.append(f"error={ContextEngine._truncate_text(error, 300)}")
        return ", ".join(fields)

    @staticmethod
    def _truncate_text(text: str, max_chars: int) -> str:
        """将文本限制在指定字符数内并显式标记截断。

        Args:
            text: 需要限制长度的原始文本。
            max_chars: 允许的最大字符数。

        Returns:
            未超限的原文，或带截断标记的文本。
        """
        if len(text) <= max_chars:
            return text
        marker = "…[已截断]"
        if max_chars <= len(marker):
            return marker[:max_chars]
        return f"{text[: max_chars - len(marker)]}{marker}"

    def _combined_context_chars(
        self,
        context: Sequence[Mapping[str, Any]],
        history_summary: str,
        reserved_chars: int,
    ) -> int:
        """估算保留历史、压缩记录和其他上下文的合计字符数。

        Args:
            context: 压缩后仍保留的原始历史。
            history_summary: 早期轮次的结构化记录。
            reserved_chars: 系统、画像、记忆和当前轮次占用字符数。

        Returns:
            合计估算字符数。
        """
        return (
            reserved_chars
            + len(history_summary)
            + self._estimate_context_chars(context)
        )

    @staticmethod
    def _estimate_context_chars(context: Sequence[Mapping[str, Any]]) -> int:
        """估算一组模型消息序列化后的字符数。

        Args:
            context: 需要估算大小的模型消息。

        Returns:
            使用紧凑 JSON 表示时的字符数。
        """
        return len(
            json.dumps(
                [dict(message) for message in context],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )

    @staticmethod
    def _get_tool_call_names(context: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        """建立工具调用 ID 到工具名称的映射。

        Args:
            context: 可能包含 assistant tool_calls 的历史消息。

        Returns:
            工具调用 ID 到 function name 的字典。
        """
        names: dict[str, str] = {}
        for message in context:
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, Mapping):
                    continue
                call_id = tool_call.get("id")
                function = tool_call.get("function")
                if not isinstance(call_id, str) or not isinstance(function, Mapping):
                    continue
                name = function.get("name")
                if isinstance(name, str):
                    names[call_id] = name
        return names

    @staticmethod
    def _is_full_read_result(message: Mapping[str, Any]) -> bool:
        """判断工具消息是否包含可清理的完整读取正文。

        Args:
            message: ``role=tool`` 的历史消息。

        Returns:
            content 是否为包含 markdown 字段的 JSON 对象。
        """
        content = message.get("content")
        if not isinstance(content, str):
            return False
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            return False
        return isinstance(result, Mapping) and isinstance(result.get("markdown"), str)

    @staticmethod
    def _compact_read_result(message: Mapping[str, Any]) -> dict[str, Any]:
        """将文档读取结果替换为可重新读取的轻量引用。

        Args:
            message: 包含完整 read_document 结果的工具消息。

        Returns:
            保留原工具调用 ID 和定位信息的压缩消息。
        """
        result = json.loads(str(message["content"]))
        index = result.get("index")
        block_ids = (
            [
                item["id"]
                for item in index
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            ]
            if isinstance(index, list)
            else []
        )
        compacted_result = {
            "tool": "read_document",
            "compacted": True,
            "path": result.get("path"),
            "revision": result.get("revision"),
            "mode": result.get("mode"),
            "view": result.get("view"),
            "returned_block_ids": block_ids,
            "next_id": result.get("next_id"),
            "metadata": result.get("metadata"),
            "warnings": result.get("warnings"),
            "recovery": "正文已从历史上下文清理，需要时请重新调用 read_document。",
        }
        compacted_message = dict(message)
        compacted_message["content"] = json.dumps(
            compacted_result,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return compacted_message

    def _get_system_context(self) -> str:
        """获取 Seshat 的基础系统指令。

        Returns:
            已渲染的基础系统提示词。
        """
        return self.system_prompt

    def _get_profile_context(self) -> str:
        """获取当前服务对象的研究与写作画像上下文。

        Returns:
            已渲染的画像提示词；当前未接入画像存储。
        """
        # TODO: 从当前租户和用户的画像存储中读取 profile 变量。
        return self._render_prompt(self.profile_prompt_path)

    def _get_memory_context(self) -> str:
        """获取与当前任务相关的长期记忆上下文。

        Returns:
            已渲染的记忆提示词；空模板返回空字符串。
        """
        # TODO: 接入记忆存储和相关性检索后，向模板传入本轮记忆。
        return self._render_prompt(self.memory_prompt_path)

    def _get_conversation_context(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> Context:
        """获取排除历史系统消息后的多轮会话上下文。

        Args:
            messages: 以前已经完成的原始会话消息。

        Returns:
            可以进入压缩流程的历史会话消息。
        """
        return [
            dict(message) for message in messages if message.get("role") != "system"
        ]

    @staticmethod
    def _merge_instruction_contexts(*contexts: str) -> str:
        """将非空的系统、画像和记忆上下文合并为一条指令。

        Args:
            *contexts: 按优先顺序排列的指令上下文。

        Returns:
            以空行分隔的非空指令文本。
        """
        return "\n\n".join(context.strip() for context in contexts if context.strip())

    def _render_prompt(self, template_path: Path, **variables: Any) -> str:
        """读取 Jinja 提示词模板并渲染运行时变量。

        Args:
            template_path: 需要读取的 UTF-8 Jinja 模板路径。
            **variables: 传入模板的运行时变量。

        Returns:
            去除首尾空白后的提示词。

        Raises:
            FileNotFoundError: 模板文件不存在。
            jinja2.UndefinedError: 模板使用了未提供且无默认值的变量。
        """
        template_source = template_path.read_text(encoding="utf-8")
        return (
            self._template_environment.from_string(template_source)
            .render(**variables)
            .strip()
        )
