"""项目类型化配置定义，负责校验 YAML 数据并提供统一的配置访问入口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_ALLOWED_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


@dataclass(frozen=True)
class IdentitySettings:
    """启动时未指定租户与用户时使用的游客身份配置。"""

    guest_tenant: str  # 游客租户标识。
    guest_user_prefix: str  # 游客用户名前缀，每次启动在其后追加时间码。


@dataclass(frozen=True)
class SessionSettings:
    """会话持久化配置。"""

    root: str  # 会话文件根目录，相对项目根目录或绝对路径；其下按租户/用户分层。


@dataclass(frozen=True)
class AgentLoopSettings:
    """AgentLoop 的运行参数。"""

    max_steps: int  # 单次用户输入允许的最大模型调用轮数。


@dataclass(frozen=True)
class BudgetSettings:
    """上下文 token 预算与估算参数。"""

    working_tokens: int  # 工作上下文软预算；超出时强制清理并警告。
    output_reserve_tokens: int  # 为模型输出预留的 token，计入物理上限校验。
    initial_chars_per_token: float  # 字符到 token 的初始换算比例。
    calibration_weight: float  # 按响应 usage 校准比例时新观测值的权重，(0, 1]。


@dataclass(frozen=True)
class CompressionSettings:
    """分级压缩参数；比例均相对 working_tokens。"""

    read_cleanup_trigger_ratio: float  # 第一级：开始清理读取正文的比例。
    read_cleanup_target_ratio: float  # 第一级：清理后一次性降到的比例。
    turn_compaction_trigger_ratio: float  # 第二级：开始归档早期轮次的比例。
    turn_compaction_target_ratio: float  # 第二级：归档后一次性降到的比例。
    keep_recent_turns: int  # 第二级至少保留原文的近期完整轮次数。
    retain_recent_turn_reads: int  # 第一级不按“轮次已结束”清理的最近轮数。
    summary_max_ratio: float  # 历史摘要允许占用的比例上限。
    cleanup_after_turn_end: bool  # 已结束轮次中未标记审阅的读取是否允许清理。


@dataclass(frozen=True)
class ReviewStateSettings:
    """审阅状态渲染进上下文时的体积约束。"""

    outline_max_level: int  # 大纲展示的最大标题级别。
    max_open_findings: int  # 展示的 open 问题最大条数，超出部分仅计数。
    finding_max_chars: int  # 单条问题展示的字符上限。


@dataclass(frozen=True)
class ContextSettings:
    """上下文引擎配置。"""

    budget: BudgetSettings  # 上下文 token 预算与估算配置。
    compression: CompressionSettings  # 历史上下文分级压缩配置。
    review_state: ReviewStateSettings  # 审阅状态注入上下文时的展示上限。


@dataclass(frozen=True)
class DocumentSettings:
    """文档结构索引与章节子任务配置。"""

    section_heading_level: int  # 划分章节使用的最大标题级别。
    summary_cache_path: str  # 章节摘要缓存文件，相对项目根目录或绝对路径。
    summary_max_chars: int  # 单个章节摘要的字符上限。
    read_default_max_chars: int  # read_document 未指定 max_chars 时的单次字符上限。
    read_max_chars: int  # read_document 允许指定的 max_chars 最大值。


@dataclass(frozen=True)
class LoggingSettings:
    """本地文件日志配置。"""

    level: str  # Python 标准日志级别名称。
    path: str  # 相对项目根目录或绝对日志文件路径。
    max_bytes: int  # 单个日志文件触发轮转前的最大字节数。
    backup_count: int  # 轮转日志保留的备份数量。


@dataclass(frozen=True)
class ModelSettings:
    """单个模型的环境变量映射与请求参数。"""

    api_key_env: str  # 保存 API Key 的环境变量名称。
    base_url_env: str  # 保存兼容 API 地址的环境变量名称。
    model_name_env: str  # 保存实际模型名称的环境变量名称。
    context_window_tokens: int  # 模型上下文窗口 token 数，用于物理上限校验。
    parameters: dict[str, Any]  # 每次模型请求默认使用的参数。


@dataclass(frozen=True)
class Settings:
    """Seshat 启动后由各组件共享的类型化项目配置。"""

    identity: IdentitySettings  # 游客身份配置。
    sessions: SessionSettings  # 会话持久化配置。
    agent_loop: AgentLoopSettings  # Agent 循环配置。
    context: ContextSettings  # 上下文组装与压缩配置。
    documents: DocumentSettings  # 文档结构索引与章节子任务配置。
    logging: LoggingSettings  # 本地文件日志配置。
    current_model: str  # 当前启用的模型配置名称。
    models: dict[str, ModelSettings]  # 以配置名称索引的模型配置。

    @property
    def active_model(self) -> ModelSettings:
        """返回当前启用的模型配置。

        Returns:
            ``current_model`` 指向的模型配置。
        """
        return self.models[self.current_model]


def parse_settings(data: Mapping[str, Any]) -> Settings:
    """把 YAML 根对象校验并转换为类型化配置。

    Args:
        data: 从项目 YAML 文件读取的根配置对象。

    Returns:
        完成类型和关联关系校验的 Settings。

    Raises:
        TypeError: 配置节点或字段类型无效。
        ValueError: 必填字段为空、数值越界或引用不存在。
    """
    identity_data = _require_mapping(data.get("identity"), "identity")
    identity = IdentitySettings(
        guest_tenant=_require_string(
            identity_data.get("guest_tenant"),
            "identity.guest_tenant",
        ),
        guest_user_prefix=_require_string(
            identity_data.get("guest_user_prefix"),
            "identity.guest_user_prefix",
        ),
    )

    sessions_data = _require_mapping(data.get("sessions"), "sessions")
    sessions = SessionSettings(
        root=_require_string(sessions_data.get("root"), "sessions.root"),
    )

    agent_loop_data = _require_mapping(data.get("agent_loop"), "agent_loop")
    agent_loop = AgentLoopSettings(
        max_steps=_require_positive_int(
            agent_loop_data.get("max_steps"),
            "agent_loop.max_steps",
        )
    )

    context_data = _require_mapping(data.get("context"), "context")
    budget_data = _require_mapping(context_data.get("budget"), "context.budget")
    budget = BudgetSettings(
        working_tokens=_require_positive_int(
            budget_data.get("working_tokens"),
            "context.budget.working_tokens",
        ),
        output_reserve_tokens=_require_positive_int(
            budget_data.get("output_reserve_tokens"),
            "context.budget.output_reserve_tokens",
        ),
        initial_chars_per_token=_require_positive_number(
            budget_data.get("initial_chars_per_token"),
            "context.budget.initial_chars_per_token",
        ),
        calibration_weight=_require_ratio(
            budget_data.get("calibration_weight"),
            "context.budget.calibration_weight",
            allow_one=True,
        ),
    )
    compression_data = _require_mapping(
        context_data.get("compression"),
        "context.compression",
    )
    compression = CompressionSettings(
        read_cleanup_trigger_ratio=_require_ratio(
            compression_data.get("read_cleanup_trigger_ratio"),
            "context.compression.read_cleanup_trigger_ratio",
        ),
        read_cleanup_target_ratio=_require_ratio(
            compression_data.get("read_cleanup_target_ratio"),
            "context.compression.read_cleanup_target_ratio",
        ),
        turn_compaction_trigger_ratio=_require_ratio(
            compression_data.get("turn_compaction_trigger_ratio"),
            "context.compression.turn_compaction_trigger_ratio",
        ),
        turn_compaction_target_ratio=_require_ratio(
            compression_data.get("turn_compaction_target_ratio"),
            "context.compression.turn_compaction_target_ratio",
        ),
        keep_recent_turns=_require_positive_int(
            compression_data.get("keep_recent_turns"),
            "context.compression.keep_recent_turns",
        ),
        retain_recent_turn_reads=_require_non_negative_int(
            compression_data.get("retain_recent_turn_reads"),
            "context.compression.retain_recent_turn_reads",
        ),
        summary_max_ratio=_require_ratio(
            compression_data.get("summary_max_ratio"),
            "context.compression.summary_max_ratio",
        ),
        cleanup_after_turn_end=_require_bool(
            compression_data.get("cleanup_after_turn_end"),
            "context.compression.cleanup_after_turn_end",
        ),
    )
    if not (
        compression.read_cleanup_target_ratio
        < compression.read_cleanup_trigger_ratio
        < compression.turn_compaction_trigger_ratio
    ):
        raise ValueError("压缩比例必须满足：第一级目标 < 第一级触发 < 第二级触发")
    if (
        compression.turn_compaction_target_ratio
        >= compression.turn_compaction_trigger_ratio
    ):
        raise ValueError("压缩比例必须满足：第二级目标 < 第二级触发")
    if compression.retain_recent_turn_reads > compression.keep_recent_turns:
        raise ValueError(
            "context.compression.retain_recent_turn_reads 不能大于 keep_recent_turns"
        )

    review_state_data = _require_mapping(
        context_data.get("review_state"),
        "context.review_state",
    )
    review_state = ReviewStateSettings(
        outline_max_level=_require_positive_int(
            review_state_data.get("outline_max_level"),
            "context.review_state.outline_max_level",
        ),
        max_open_findings=_require_positive_int(
            review_state_data.get("max_open_findings"),
            "context.review_state.max_open_findings",
        ),
        finding_max_chars=_require_positive_int(
            review_state_data.get("finding_max_chars"),
            "context.review_state.finding_max_chars",
        ),
    )

    documents_data = _require_mapping(data.get("documents"), "documents")
    documents = DocumentSettings(
        section_heading_level=_require_positive_int(
            documents_data.get("section_heading_level"),
            "documents.section_heading_level",
        ),
        summary_cache_path=_require_string(
            documents_data.get("summary_cache_path"),
            "documents.summary_cache_path",
        ),
        summary_max_chars=_require_positive_int(
            documents_data.get("summary_max_chars"),
            "documents.summary_max_chars",
        ),
        read_default_max_chars=_require_positive_int(
            documents_data.get("read_default_max_chars"),
            "documents.read_default_max_chars",
        ),
        read_max_chars=_require_positive_int(
            documents_data.get("read_max_chars"),
            "documents.read_max_chars",
        ),
    )
    if documents.read_default_max_chars > documents.read_max_chars:
        raise ValueError("documents.read_default_max_chars 不能大于 read_max_chars")

    logging_data = _require_mapping(data.get("logging"), "logging")
    level = _require_string(logging_data.get("level"), "logging.level").upper()
    if level not in _ALLOWED_LOG_LEVELS:
        raise ValueError(f"不支持的日志级别: {level}")
    logging_settings = LoggingSettings(
        level=level,
        path=_require_string(logging_data.get("path"), "logging.path"),
        max_bytes=_require_positive_int(
            logging_data.get("max_bytes"),
            "logging.max_bytes",
        ),
        backup_count=_require_non_negative_int(
            logging_data.get("backup_count"),
            "logging.backup_count",
        ),
    )

    current_model = _require_string(data.get("current_model"), "current_model")
    models_data = _require_mapping(data.get("models"), "models")
    models = {
        str(model_name): _parse_model_settings(str(model_name), model_data)
        for model_name, model_data in models_data.items()
    }
    if current_model not in models:
        raise ValueError(f"current_model 未在 models 中定义: {current_model}")
    window_tokens = models[current_model].context_window_tokens
    if budget.working_tokens + budget.output_reserve_tokens >= window_tokens:
        raise ValueError(
            "context.budget.working_tokens 与 output_reserve_tokens 之和"
            f"必须小于当前模型的 context_window_tokens: {window_tokens}"
        )

    return Settings(
        identity=identity,
        sessions=sessions,
        agent_loop=agent_loop,
        context=ContextSettings(
            budget=budget,
            compression=compression,
            review_state=review_state,
        ),
        documents=documents,
        logging=logging_settings,
        current_model=current_model,
        models=models,
    )


def _parse_model_settings(model_name: str, value: Any) -> ModelSettings:
    """解析单个模型配置。

    Args:
        model_name: 当前模型配置名称，用于生成精确错误信息。
        value: YAML 中的模型配置节点。

    Returns:
        类型化模型配置。
    """
    prefix = f"models.{model_name}"
    model = _require_mapping(value, prefix)
    parameters = _require_mapping(model.get("parameters", {}), f"{prefix}.parameters")
    return ModelSettings(
        api_key_env=_require_string(model.get("api_key_env"), f"{prefix}.api_key_env"),
        base_url_env=_require_string(
            model.get("base_url_env"),
            f"{prefix}.base_url_env",
        ),
        model_name_env=_require_string(
            model.get("model_name_env"),
            f"{prefix}.model_name_env",
        ),
        context_window_tokens=_require_positive_int(
            model.get("context_window_tokens"),
            f"{prefix}.context_window_tokens",
        ),
        parameters=dict(parameters),
    )


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    """校验配置节点为键值对象。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。

    Returns:
        通过校验的映射对象。

    Raises:
        TypeError: 配置值不是映射对象。
    """
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} 必须是对象")
    return value


def _require_string(value: Any, path: str) -> str:
    """校验配置值为非空字符串。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。

    Returns:
        去除首尾空白后的字符串。

    Raises:
        TypeError: 配置值不是字符串。
        ValueError: 字符串为空。
    """
    if not isinstance(value, str):
        raise TypeError(f"{path} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{path} 不能为空")
    return normalized


def _require_positive_int(value: Any, path: str) -> int:
    """校验配置值为正整数。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。

    Returns:
        通过校验的正整数。

    Raises:
        TypeError: 配置值不是整数。
        ValueError: 整数不大于零。
    """
    result = _require_int(value, path)
    if result <= 0:
        raise ValueError(f"{path} 必须大于 0")
    return result


def _require_non_negative_int(value: Any, path: str) -> int:
    """校验配置值为非负整数。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。

    Returns:
        通过校验的非负整数。

    Raises:
        TypeError: 配置值不是整数。
        ValueError: 整数小于零。
    """
    result = _require_int(value, path)
    if result < 0:
        raise ValueError(f"{path} 不能小于 0")
    return result


def _require_int(value: Any, path: str) -> int:
    """校验配置值为整数且排除布尔值。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。

    Returns:
        通过校验的整数。

    Raises:
        TypeError: 配置值不是整数。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{path} 必须是整数")
    return value


def _require_positive_number(value: Any, path: str) -> float:
    """校验配置值为正数（整数或浮点数，排除布尔值）。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。

    Returns:
        通过校验的浮点数。

    Raises:
        TypeError: 配置值不是数字。
        ValueError: 数值不大于零。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{path} 必须是数字")
    if value <= 0:
        raise ValueError(f"{path} 必须大于 0")
    return float(value)


def _require_ratio(value: Any, path: str, *, allow_one: bool = False) -> float:
    """校验配置值为 (0, 1) 区间内的比例；allow_one 时允许等于 1。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。
        allow_one: 是否允许取值 1。

    Returns:
        通过校验的比例。

    Raises:
        TypeError: 配置值不是数字。
        ValueError: 比例越界。
    """
    ratio = _require_positive_number(value, path)
    if ratio > 1 or (ratio == 1 and not allow_one):
        upper = "不大于 1" if allow_one else "小于 1"
        raise ValueError(f"{path} 必须{upper}")
    return ratio


def _require_bool(value: Any, path: str) -> bool:
    """校验配置值为布尔值。

    Args:
        value: 待校验的配置值。
        path: 用于错误信息的配置路径。

    Returns:
        通过校验的布尔值。

    Raises:
        TypeError: 配置值不是布尔值。
    """
    if not isinstance(value, bool):
        raise TypeError(f"{path} 必须是布尔值")
    return value
