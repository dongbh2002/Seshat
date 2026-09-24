"""项目配置包，在首次导入时加载 YAML，并向其他模块提供共享配置。"""

from backend.config.config import load_config


config = load_config()

__all__ = ["config", "load_config"]
