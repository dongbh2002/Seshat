"""装配包：按配置创建并组装各组件，供 CLI 等入口使用。"""

from backend.bootstrap.paths import PROJECT_ROOT, TENANT_PACKS_ROOT
from backend.bootstrap.runtime_factory import create_default_runtime, create_runtime

__all__ = [
    "PROJECT_ROOT",
    "TENANT_PACKS_ROOT",
    "create_default_runtime",
    "create_runtime",
]
