"""项目配置加载模块，负责读取 YAML 并构造类型化 Settings。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from backend.config.settings import Settings, parse_settings

_DEFAULT_CONFIG_PATH = Path(__file__).with_name("application_local.yaml")


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """读取 YAML 项目配置。

    Args:
        config_path: 可选的 YAML 文件路径；未传入时读取默认本地配置。

    Returns:
        YAML 根节点对应的配置字典。
    """
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}
    if not isinstance(data, dict):
        raise TypeError("YAML 配置根节点必须是对象")
    return data


def load_settings(config_path: Path | None = None) -> Settings:
    """读取 YAML 项目配置并转换为类型化 Settings。

    Args:
        config_path: 可选的 YAML 文件路径；未传入时读取默认本地配置。

    Returns:
        完成校验的项目 Settings 对象。

    Raises:
        FileNotFoundError: 配置文件不存在。
        TypeError: YAML 根节点或配置字段类型无效。
        ValueError: 配置字段为空、越界或引用不存在。
    """
    return parse_settings(load_config(config_path))
