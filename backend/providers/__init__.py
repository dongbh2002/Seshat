"""模型供应商包，对外提供通用模型客户端创建函数。"""

from backend.providers.client import create_model_client

__all__ = ["create_model_client"]
