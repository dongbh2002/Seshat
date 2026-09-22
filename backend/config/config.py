"""项目配置加载模块，仅负责读取 YAML 配置文件。"""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml


_DEFAULT_CONFIG_PATH = Path(__file__).with_name("application_local.yaml")


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 YAML 项目配置。

    Args:
        config_path: 可选的 YAML 文件路径；未传入时读取默认本地配置。

    Returns:
        YAML 根节点对应的配置字典。
    """
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}
