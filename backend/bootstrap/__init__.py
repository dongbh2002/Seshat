"""装配包：解析启动身份，按配置创建并组装各组件，供 CLI 等入口使用。"""

from backend.bootstrap.identity import Identity, resolve_identity
from backend.bootstrap.paths import (
    PROJECT_ROOT,
    resolve_project_path,
    session_directory,
    workspace_directory,
)
from backend.bootstrap.runtime_factory import (
    create_default_runtime,
    create_runtime,
    create_session_manager,
)

__all__ = [
    "PROJECT_ROOT",
    "Identity",
    "create_default_runtime",
    "create_runtime",
    "create_session_manager",
    "resolve_identity",
    "resolve_project_path",
    "session_directory",
    "workspace_directory",
]
