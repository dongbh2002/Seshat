"""项目类型化配置定义，负责校验 YAML 数据并提供统一的配置访问入口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_ALLOWED_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


@dataclass(frozen=True)
class TenantSettings:
    """单个租户的默认用户与会话配置。"""

    default_user: str  # 租户下默认使用的用户标识。
    session_name: str  # 默认会话名称。


@dataclass(frozen=True)
class IdentitySettings:
    """已经解析完成的默认租户、用户和会话身份。"""

    tenant_id: str  # 默认租户标识。
    user_id: str  # 默认用户标识。
    session_name: str  # 默认会话名称。


@dataclass(frozen=True)
class AgentLoopSettings:
    """AgentLoop 的运行参数。"""

    max_steps: int  # 单次用户输入允许的最大模型调用轮数。


@dataclass(frozen=True)
class CompressionSettings:
    """历史上下文的分级压缩参数。"""

    tool_cleanup_threshold_chars: int  # 开始清理旧工具结果的字符阈值。
    turn_compaction_threshold_chars: int  # 开始压实早期对话的字符阈值。
    hard_limit_chars: int  # 最终上下文允许的字符硬上限。
    keep_recent_turns: int  # 压实时保留原文的近期完整轮次数。
    summary_max_chars: int  # 结构化历史摘要允许的最大字符数。


@dataclass(frozen=True)
class ContextSettings:
    """上下文引擎配置。"""

    compression: CompressionSettings  # 历史上下文分级压缩配置。


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
    parameters: dict[str, Any]  # 每次模型请求默认使用的参数。


@dataclass(frozen=True)
class Settings:
    """Seshat 启动后由各组件共享的类型化项目配置。"""

    default_tenant: str  # 默认租户标识。
    tenants: dict[str, TenantSettings]  # 以租户标识索引的租户配置。
    agent_loop: AgentLoopSettings  # Agent 循环配置。
    context: ContextSettings  # 上下文组装与压缩配置。
    logging: LoggingSettings  # 本地文件日志配置。
    current_model: str  # 当前启用的模型配置名称。
    models: dict[str, ModelSettings]  # 以配置名称索引的模型配置。

    @property
    def default_identity(self) -> IdentitySettings:
        """返回默认租户对应的用户和会话身份。

        Returns:
            已解析完成的默认身份配置。
        """
        tenant = self.tenants[self.default_tenant]
        return IdentitySettings(
            tenant_id=self.default_tenant,
            user_id=tenant.default_user,
            session_name=tenant.session_name,
        )

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
    default_tenant = _require_string(data.get("default_tenant"), "default_tenant")
    tenants_data = _require_mapping(data.get("tenants"), "tenants")
    tenants = {
        str(tenant_id): _parse_tenant_settings(str(tenant_id), tenant_data)
        for tenant_id, tenant_data in tenants_data.items()
    }
    if default_tenant not in tenants:
        raise ValueError(f"default_tenant 未在 tenants 中定义: {default_tenant}")

    agent_loop_data = _require_mapping(data.get("agent_loop"), "agent_loop")
    agent_loop = AgentLoopSettings(
        max_steps=_require_positive_int(
            agent_loop_data.get("max_steps"),
            "agent_loop.max_steps",
        )
    )

    context_data = _require_mapping(data.get("context"), "context")
    compression_data = _require_mapping(
        context_data.get("compression"),
        "context.compression",
    )
    compression = CompressionSettings(
        tool_cleanup_threshold_chars=_require_positive_int(
            compression_data.get("tool_cleanup_threshold_chars"),
            "context.compression.tool_cleanup_threshold_chars",
        ),
        turn_compaction_threshold_chars=_require_positive_int(
            compression_data.get("turn_compaction_threshold_chars"),
            "context.compression.turn_compaction_threshold_chars",
        ),
        hard_limit_chars=_require_positive_int(
            compression_data.get("hard_limit_chars"),
            "context.compression.hard_limit_chars",
        ),
        keep_recent_turns=_require_positive_int(
            compression_data.get("keep_recent_turns"),
            "context.compression.keep_recent_turns",
        ),
        summary_max_chars=_require_positive_int(
            compression_data.get("summary_max_chars"),
            "context.compression.summary_max_chars",
        ),
    )
    if not (
        compression.tool_cleanup_threshold_chars
        < compression.turn_compaction_threshold_chars
        < compression.hard_limit_chars
    ):
        raise ValueError("上下文压缩阈值必须按工具清理、轮次压实、硬上限递增")

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

    return Settings(
        default_tenant=default_tenant,
        tenants=tenants,
        agent_loop=agent_loop,
        context=ContextSettings(compression=compression),
        logging=logging_settings,
        current_model=current_model,
        models=models,
    )


def _parse_tenant_settings(tenant_id: str, value: Any) -> TenantSettings:
    """解析单个租户配置。

    Args:
        tenant_id: 当前租户标识，用于生成精确错误信息。
        value: YAML 中的租户配置节点。

    Returns:
        类型化租户配置。
    """
    tenant = _require_mapping(value, f"tenants.{tenant_id}")
    return TenantSettings(
        default_user=_require_string(
            tenant.get("default_user"),
            f"tenants.{tenant_id}.default_user",
        ),
        session_name=_require_string(
            tenant.get("session_name"),
            f"tenants.{tenant_id}.session_name",
        ),
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
