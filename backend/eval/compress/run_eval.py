"""压缩机制评估入口：预算 × 行为 × 策略逐一在真实 Runtime 上跑剧本，输出 report.md 与 results.json。

用法：``uv run python -m backend.eval.compress.run_eval [--config 路径]``
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from backend.bootstrap import PROJECT_ROOT, create_runtime
from backend.config import load_config
from backend.config.settings import parse_settings
from backend.eval.compress.baselines import (
    ClearToolResults,
    NoCompression,
    SlidingWindow,
)
from backend.eval.compress.metrics import (
    DocumentTruth,
    RunTrace,
    StepRecord,
    available,
    duplicate_chars,
    has_stale_copy,
    prefix_reuse,
    request_chars,
    serialize_request,
    stale_block_count,
    summarize_trace,
    visible_blocks,
)
from backend.eval.compress.scripted_agent import (
    CONSTRAINT_KEY,
    ModelAction,
    ScriptedAgent,
    ScriptedModelClient,
)
from backend.hooks import HookContext, HookEvent
from backend.runtime import Runtime
from backend.runtime.context.compression import (
    CompressionResult,
    CompressionStrategy,
    HistoryArchive,
)
from backend.utils.docx import DocumentIndex

_DEFAULT_CONFIG = Path(__file__).with_name("eval_config.yaml")  # 默认评估配置。
_BASELINES = {  # 对照组策略类型到实现类。
    "no_compression": NoCompression,
    "sliding_window": SlidingWindow,
    "clear_tool_results": ClearToolResults,
}
_TIERED = "tiered"  # 被测分级策略的类型名。
_STEP_FIELDS = (  # results.json 中每步保留的压缩统计字段。
    "tokens_before",
    "tokens_after",
    "read_cleanup_triggered",
    "turn_compaction_triggered",
    "budget_enforced",
    "active_turn_forced",
    "cleaned_blocks",
    "compacted_reads",
    "archived_turns",
    "dropped_summary_entries",
    "unrecorded_reads",
    "forced_reads",
)


class TimedCompression(CompressionStrategy):
    """包装任意压缩策略，记录最近一次压缩耗时。"""

    def __init__(self, inner: CompressionStrategy) -> None:
        """初始化计时包装。

        Args:
            inner: 被计时的压缩策略。

        Returns:
            None。
        """
        self.inner = inner  # 被计时的压缩策略。
        self.last_ms = 0.0  # 最近一次压缩耗时（毫秒）。

    def compress(
        self,
        history: Sequence[Mapping[str, Any]],
        active_turn: Sequence[Mapping[str, Any]],
        *,
        archive: HistoryArchive,
        reserved_tokens: int,
    ) -> CompressionResult:
        """调用被包装策略并计时。

        Args:
            history: 历史消息。
            active_turn: 当前轮次消息。
            archive: 历史归档。
            reserved_tokens: 指令等预留占用。

        Returns:
            被包装策略的压缩结果。
        """
        start = time.perf_counter()
        result = self.inner.compress(
            history,
            active_turn,
            archive=archive,
            reserved_tokens=reserved_tokens,
        )
        self.last_ms = (time.perf_counter() - start) * 1000
        return result


class ReportCapture:
    """在模型请求前抓取 ContextEngine 给出的压缩统计。"""

    def __init__(self) -> None:
        """初始化抓取器。

        Returns:
            None。
        """
        self.latest: dict[str, Any] = {}  # 最近一次请求的压缩统计。

    def __call__(self, context: HookContext) -> None:
        """保存本次请求的 metadata["context"]。

        Args:
            context: 模型请求前的生命周期上下文。

        Returns:
            None。
        """
        self.latest = dict(context.metadata.get("context") or {})


class EvalRecorder:
    """在每次模型请求时测量上下文，并统计重读与作答依据。"""

    def __init__(
        self,
        agent: ScriptedAgent,
        truth: DocumentTruth,
        chars_per_token: float,
        capture: ReportCapture,
    ) -> None:
        """初始化记录器。

        Args:
            agent: 脚本化模型，提供当前轮次与所需块。
            truth: 文档当前版本。
            chars_per_token: 折算真实 token 的字符/token 比例。
            capture: 压缩统计抓取器。

        Returns:
            None。
        """
        self.agent = agent  # 脚本化模型。
        self.truth = truth  # 文档当前版本。
        self.chars_per_token = chars_per_token  # 字符/token 比例。
        self.capture = capture  # 压缩统计抓取器。
        self.timer: TimedCompression | None = None  # 压缩计时包装，安装策略后设置。
        self.trace = RunTrace()  # 本次运行的测量。
        self._previous_request: str | None = None  # 上一次请求的序列化文本。
        self._visible: list[tuple[str, str]] = []  # 本次请求中可见的块正文。
        self._pending_rereads: set[str] = set()  # 结果尚未出现的重读调用 ID。
        self._saw_current = False  # 本轮是否有某一步看到了所需块的全部当前版本。
        self._saw_stale = False  # 本轮是否有某一步看到了所需块的过时副本。

    def on_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> None:
        """测量一次模型请求。

        Args:
            messages: 请求消息。
            tools: 工具定义。

        Returns:
            None。
        """
        self.truth.refresh()
        agent = self.agent
        label = agent.turn.label if agent.turn else ""
        self._visible = visible_blocks(messages)
        for message in messages:
            call_id = message.get("tool_call_id")
            if message.get("role") == "tool" and call_id in self._pending_rereads:
                self._pending_rereads.discard(call_id)
                self.trace.reread_tokens += self._tokens(
                    len(str(message.get("content")))
                )
        if agent.turn_start:
            self._saw_current = self._saw_stale = False
        if agent.turn_needs:
            hit = available(self._visible, self.truth, agent.turn_needs)
            if agent.turn_start:
                self.trace.needs.append((label, hit))
            self._saw_current |= hit
            self._saw_stale |= has_stale_copy(
                self._visible, self.truth, agent.turn_needs
            )
        if agent.turn_start and agent.turn and agent.turn.recall_keys:
            text = _dialogue_text(messages)
            keys = agent.turn.recall_keys
            self.trace.recall = sum(key in text for key in keys) / len(keys)
        serialized = serialize_request(messages, tools)
        self.trace.steps.append(
            StepRecord(
                turn=label,
                prompt_tokens=self._tokens(request_chars(messages, tools)),
                stale_blocks=stale_block_count(self._visible, self.truth),
                duplicate_tokens=self._tokens(duplicate_chars(self._visible)),
                constraint_present=(
                    CONSTRAINT_KEY in _all_text(messages)
                    if agent.turn_index > agent.constraint_turn
                    else None
                ),
                prefix_reuse=prefix_reuse(self._previous_request, serialized),
                compress_ms=self.timer.last_ms if self.timer else 0.0,
                report=dict(self.capture.latest),
            )
        )
        self._previous_request = serialized

    def on_action(self, action: ModelAction) -> None:
        """统计重读调用；最终回复时判定本轮作答依据是否正确。

        作答依据正确：本轮某一步看到过所需块的全部当前版本，且任何一步都没有
        看到这些块的过时副本。模型在看到正文的那一步完成分析（合规行为随即
        标记已审阅），因此不要求回复那一步正文仍在。

        Args:
            action: 脚本化模型的本步动作。

        Returns:
            None。
        """
        self._pending_rereads.update(action.reread_ids)
        self.trace.reread_count += len(action.reread_ids)
        if not action.tool_calls and self.agent.turn_needs:
            label = self.agent.turn.label if self.agent.turn else ""
            self.trace.answers.append(
                (label, self._saw_current and not self._saw_stale)
            )

    def _tokens(self, chars: int) -> int:
        """把字符数折算为 token。

        Args:
            chars: 字符数。

        Returns:
            向上取整的 token 数。
        """
        return math.ceil(chars / self.chars_per_token)


def run_once(
    eval_config: Mapping[str, Any],
    base_config: Mapping[str, Any],
    *,
    budget: int,
    behavior: str,
    strategy_name: str,
) -> dict[str, Any]:
    """在文档副本上跑一次完整剧本。

    Args:
        eval_config: 评估配置。
        base_config: 系统 YAML 配置字典（不修改）。
        budget: 本次 working_tokens。
        behavior: 脚本化模型行为。
        strategy_name: eval_config.strategies 中的策略名。

    Returns:
        含参数、汇总指标与逐步测量的字典。
    """
    strategy = eval_config["strategies"][strategy_name]
    overrides = {
        "context.budget.working_tokens": budget,
        **(strategy.get("overrides") or {}),
    }
    settings = parse_settings(_apply_overrides(base_config, overrides))
    source = PROJECT_ROOT / eval_config["document"]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copyfile(source, root / source.name)
        truth = DocumentTruth(root / source.name)
        agent = ScriptedAgent(
            DocumentIndex(root, settings.documents.section_heading_level),
            truth,
            source.name,
            behavior,
            eval_config["answer_chars"],
        )
        capture = ReportCapture()
        ratio = eval_config["usage_chars_per_token"]
        recorder = EvalRecorder(agent, truth, ratio, capture)
        client = ScriptedModelClient(
            agent,
            ratio,
            observer=recorder.on_request,
            action_observer=recorder.on_action,
        )
        runtime = create_runtime(
            settings,
            document_root=root,
            client=client,  # type: ignore[arg-type]
            model="scripted",
        )
        recorder.timer = _install_strategy(runtime, strategy["kind"], budget)
        runtime.hook_engine.register(
            HookEvent.BEFORE_MODEL_REQUEST, capture, critical=False
        )
        for index in range(len(agent.turns)):
            try:
                runtime.run(agent.start_turn(index))
            except Exception as error:  # noqa: BLE001 - 记录失败原因后继续下一组评估。
                recorder.trace.error = f"{agent.turns[index].label}: {error}"
                break
        recorder.trace.history_messages = len(runtime.agent_loop.messages)
        recorder.trace.edit_applied = agent.edited_block is not None
    trace = recorder.trace
    return {
        "budget": budget,
        "behavior": behavior,
        "strategy": strategy_name,
        "metrics": summarize_trace(trace, budget),
        "needs": trace.needs,
        "answers": trace.answers,
        "steps": [
            {
                **{
                    key: value for key, value in asdict(step).items() if key != "report"
                },
                "report": {key: step.report.get(key) for key in _STEP_FIELDS},
            }
            for step in trace.steps
        ],
    }


def _install_strategy(runtime: Runtime, kind: str, budget: int) -> TimedCompression:
    """按类型替换压缩策略（tiered 保留装配结果），并包上计时。

    Args:
        runtime: 已装配的 Runtime。
        kind: 策略类型。
        budget: working_tokens。

    Returns:
        已安装的计时包装。

    Raises:
        ValueError: 策略类型未知。
    """
    engine = runtime.agent_loop.context_engine
    if kind == _TIERED:
        inner = engine.compression
    elif kind in _BASELINES:
        inner = _BASELINES[kind](budget, engine.estimator)
    else:
        raise ValueError(f"未知策略类型: {kind}")
    timer = TimedCompression(inner)
    engine.compression = timer
    return timer


def _apply_overrides(
    config: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """按点号路径覆盖配置字典的副本。

    Args:
        config: 原配置。
        overrides: 点号路径到新值。

    Returns:
        覆盖后的新字典。

    Raises:
        KeyError: 路径在原配置中不存在（避免拼错路径却静默生效）。
    """
    result = copy.deepcopy(dict(config))
    for dotted, value in overrides.items():
        node = result
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        if leaf not in node:
            raise KeyError(f"配置路径不存在: {dotted}")
        node[leaf] = value
    return result


def _all_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """拼接全部消息文本。

    Args:
        messages: 请求消息。

    Returns:
        拼接后的文本。
    """
    return "\n".join(str(message.get("content") or "") for message in messages)


def _dialogue_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """拼接除工具结果外的消息文本（用户、回复、系统摘要与审阅状态）。

    Args:
        messages: 请求消息。

    Returns:
        拼接后的文本。
    """
    return _all_text([m for m in messages if m.get("role") != "tool"])


# ---- 报告 ----

_COST_COLUMNS = (  # 成本表：(指标键, 表头, 格式)。
    ("total_prompt_tokens", "总输入 token", "int"),
    ("uncached_tokens_est", "估计未缓存 token", "int"),
    ("peak_prompt_tokens", "峰值 token", "int"),
    ("over_budget_steps", "超预算步数", "int"),
    ("steps", "模型调用", "int"),
    ("reread_count", "重读次数", "int"),
    ("reread_tokens", "重读 token", "int"),
    ("prefix_reuse_mean", "前缀复用", "pct"),
    ("compress_ms_mean", "压缩耗时 ms", "float"),
)
_QUALITY_COLUMNS = (  # 质量表：(指标键, 表头, 格式)。
    ("need_hit_rate", "需求命中", "pct"),
    ("answer_grounded_rate", "作答依据正确", "pct"),
    ("stale_exposure_steps", "过时暴露步数", "int"),
    ("duplicate_tokens_mean", "平均重复 token", "float"),
    ("constraint_retention", "约束保留", "pct"),
    ("recall", "回顾召回", "pct"),
    ("edit_applied", "编辑成功", "bool"),
    ("forced_warning_steps", "强制清理告警", "int"),
)
_TIERED_COLUMNS = (  # 分级策略触发表：(指标键, 表头, 格式)。
    ("l1_steps", "L1 触发步数", "int"),
    ("l2_steps", "L2 触发步数", "int"),
    ("l3_steps", "L3 触发步数", "int"),
    ("history_messages", "结束时历史条数", "int"),
)
_DEFINITIONS = """\
- 总输入 token：全部模型调用的输入 token 之和（按 usage 比例由字符折算），直接对应成本。
- 峰值 token：单次调用最大输入；超预算步数：输入超过 working_tokens 的调用数。
- 重读次数 / 重读 token：所需正文不在上下文、模型不得不重新读取的调用数与读回的 token。
- 前缀复用：相邻两次请求（工具定义 + 消息）最长公共前缀占比，前缀缓存命中率的代理指标。
- 估计未缓存 token：每次调用的输入 token ×（1 − 前缀复用）之和，近似开启前缀缓存后按原价计费的部分。
- 需求命中：每轮开始时，本轮所需块的**当前版本**是否已全部在上下文中（不重读就能答）。
- 作答依据正确：本轮中模型某一步看到过所需块的全部当前版本，且任何一步都没看到这些块的过时副本。
- 过时暴露步数：上下文中出现与文档当前版本不一致的块正文的调用数。
- 平均重复 token：同一块同一版本正文在上下文中重复出现所占的 token（每步平均）。
- 约束保留：用户提出约束之后，每次调用上下文中仍包含该约束的比例。
- 回顾召回：最后一轮时，之前 9 轮用户请求原文仍出现在（非工具结果的）上下文中的比例。
- 强制清理告警：分级策略在模型记录审阅前被迫清理正文、并向模型发出警告的调用数。
"""
_LIMITATIONS = """\
- 模型是脚本化的：行为固定且可复现，能隔离“压缩策略”这一变量，但不代表真实 LLM 的决策质量；
  真实模型在上下文被清理后是否会重读、是否会被过时正文误导，需要线上日志或真实模型评估补充。
- token 按“字符 ÷ 固定比例”折算，与真实分词器有偏差；各策略口径一致，对比结论不受影响。
- 评估文档为英文模板，章节较短；用缩小 working_tokens 的方式模拟“文档远大于预算”的长论文场景。
- 前缀复用只是缓存友好度的代理指标，实际命中还取决于服务商的缓存粒度。
- 信息可用性按“块正文逐字出现在上下文中”判断，不评估摘要或审阅状态对理解的帮助。
"""


def write_report(
    runs: list[dict[str, Any]],
    eval_config: Mapping[str, Any],
    document: Mapping[str, Any],
    output_dir: Path,
) -> None:
    """写出 results.json 与 report.md。

    Args:
        runs: 每次运行的结果。
        eval_config: 评估配置。
        document: 评估文档的规模信息。
        output_dir: 输出目录。

    Returns:
        None。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        json.dumps(
            {"config": eval_config, "document": document, "runs": runs},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    lines = [
        "# 上下文压缩评估报告",
        "",
        "## 方法",
        "",
        "- 在真实 Runtime（真实工具、Hook、ContextEngine）上，用脚本化模型跑固定的 10 轮剧本："
        "通读全文 → 提约束 → 问章节 → 修订式编辑 → 问另一章 → 查看改动 → 检查章节并遵守约束 → "
        "审阅最大章 → 回到被改过的章节 → 回顾全程。",
        "- 模型只依据每次请求中实际可见的内容行动：所需块不在上下文就按块 ID 重读；"
        "看得到就直接使用（与真实模型一样无法分辨过时正文）。",
        "- 行为 compliant 会标记已审阅、记录问题和用户约束；noncompliant 从不记录，考察模型不配合时的退化。",
        "- 对照组：no_compression（不压缩）、sliding_window（超预算整轮丢弃最旧历史）、"
        "clear_tool_results（历史工具结果一律清空，再滑动窗口）；"
        "tiered_no_retain 为消融：不保留最近一轮的读取。",
        f"- 文档：{document['name']}，{document['blocks']} 个块，{document['chars']} 字符，"
        f"约 {document['tokens']} token；每轮回复 {eval_config['answer_chars']} 字符。",
        "",
        "## 指标定义",
        "",
        _DEFINITIONS,
    ]
    budgets = sorted({run["budget"] for run in runs})
    behaviors = list(dict.fromkeys(run["behavior"] for run in runs))
    for budget in budgets:
        ratio = document["tokens"] / budget
        lines += ["", f"## working_tokens = {budget}（文档约为预算的 {ratio:.1f} 倍）"]
        for behavior in behaviors:
            group = [
                r for r in runs if r["budget"] == budget and r["behavior"] == behavior
            ]
            lines += ["", f"### {behavior}", ""]
            lines += _table(group, _COST_COLUMNS)
            lines += [""]
            lines += _table(group, _QUALITY_COLUMNS)
            tiered = [
                r
                for r in group
                if eval_config["strategies"][r["strategy"]]["kind"] == _TIERED
            ]
            if tiered:
                lines += [""]
                lines += _table(tiered, _TIERED_COLUMNS)
            errors = [r for r in group if r["metrics"]["error"]]
            for run in errors:
                lines.append(f"\n- {run['strategy']} 中断：{run['metrics']['error']}")
    lines += ["", "## 局限", "", _LIMITATIONS]
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _table(
    runs: list[dict[str, Any]],
    columns: Sequence[tuple[str, str, str]],
) -> list[str]:
    """把一组运行渲染为 Markdown 表格，每行一个策略。

    Args:
        runs: 同一预算与行为下的运行。
        columns: (指标键, 表头, 格式) 列定义。

    Returns:
        表格行。
    """
    header = "| 策略 | " + " | ".join(title for _, title, _ in columns) + " |"
    divider = "|---" * (len(columns) + 1) + "|"
    rows = [
        f"| {run['strategy']} | "
        + " | ".join(_format(run["metrics"].get(key), kind) for key, _, kind in columns)
        + " |"
        for run in runs
    ]
    return [header, divider, *rows]


def _format(value: Any, kind: str) -> str:
    """按列格式渲染指标值。

    Args:
        value: 指标值。
        kind: int、float、pct 或 bool。

    Returns:
        渲染后的文本；缺失值为“—”。
    """
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value:.0%}"
    if kind == "float":
        return f"{value:.1f}"
    if kind == "bool":
        return "是" if value else "否"
    return f"{value:,}"


def main() -> None:
    """读取评估配置，跑全部组合并写出报告。

    Returns:
        None。
    """
    parser = argparse.ArgumentParser(description="上下文压缩机制评估")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    arguments = parser.parse_args()
    with arguments.config.open(encoding="utf-8") as file:
        eval_config = yaml.safe_load(file)
    base_config = load_config()
    source = PROJECT_ROOT / eval_config["document"]
    truth = DocumentTruth(source)
    chars = sum(len(block.to_markdown()) for block in truth.blocks.values())
    document = {
        "name": source.name,
        "blocks": len(truth.blocks),
        "chars": chars,
        "tokens": math.ceil(chars / eval_config["usage_chars_per_token"]),
    }
    runs = []
    for budget in eval_config["budgets"]:
        for behavior in eval_config["behaviors"]:
            for name in eval_config["strategies"]:
                run = run_once(
                    eval_config,
                    base_config,
                    budget=budget,
                    behavior=behavior,
                    strategy_name=name,
                )
                metrics = run["metrics"]
                print(
                    f"{budget:>6} {behavior:<13} {name:<19} "
                    f"total={metrics['total_prompt_tokens']:>8,} "
                    f"hit={_format(metrics['need_hit_rate'], 'pct'):>5} "
                    f"grounded={_format(metrics['answer_grounded_rate'], 'pct'):>5} "
                    f"error={metrics['error']}"
                )
                runs.append(run)
    output_dir = PROJECT_ROOT / eval_config["output_dir"]
    write_report(runs, eval_config, document, output_dir)
    print(f"报告已写入 {output_dir}")


if __name__ == "__main__":
    main()
