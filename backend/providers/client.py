"""通用模型客户端创建模块，根据注入配置读取对应的环境变量。"""

import os
from pathlib import Path
from typing import Optional, Tuple

from dotenv import dotenv_values
from openai import OpenAI

from backend.config import Settings


_DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / "env" / ".env"


def create_model_client(
    settings: Settings,
    env_path: Optional[Path] = None,
) -> Tuple[OpenAI, str]:
    """按类型化项目配置创建客户端并取得实际模型名称。

    Args:
        settings: 启动入口加载并注入的类型化项目配置。
        env_path: 可选的 `.env` 路径；未传入时读取仓库默认文件。

    Returns:
        OpenAI 兼容客户端与实际模型名称组成的元组。
    """
    path = Path(env_path) if env_path else _DEFAULT_ENV_PATH
    environment = {
        key: value for key, value in dotenv_values(path).items() if value is not None
    }
    environment.update(os.environ)

    model_config = settings.active_model
    client = OpenAI(
        api_key=environment[model_config.api_key_env],
        base_url=environment[model_config.base_url_env],
    )
    model_name = environment[model_config.model_name_env]
    return client, model_name
