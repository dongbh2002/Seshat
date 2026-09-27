"""装配使用的路径：项目根目录、配置路径解析，以及运行时数据目录的布局。

数据根目录（yaml data.root）下的布局：
    workspaces/<tenant_id>/<user_id>/   待处理文档，工具只能访问这里
    sessions/<tenant_id>/<user_id>/     会话文件
"""

from __future__ import annotations

from pathlib import Path

from backend.bootstrap.identity import Identity
from backend.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 项目根目录。
WORKSPACES_DIRNAME = "workspaces"  # 数据根目录下存放各用户工作区的子目录名。
SESSIONS_DIRNAME = "sessions"  # 数据根目录下存放各用户会话文件的子目录名。


def resolve_project_path(path: str) -> Path:
    """把配置中的路径解析为绝对路径，相对路径以项目根目录为基准。

    Args:
        path: 配置中的相对或绝对路径。

    Returns:
        绝对路径。
    """
    configured = Path(path)
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def workspace_directory(settings: Settings, identity: Identity) -> Path:
    """返回身份对应的工作区目录，即工具允许访问的文档目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        identity: 租户与用户身份。

    Returns:
        <data.root>/workspaces/<tenant_id>/<user_id> 的绝对路径（不保证存在）。
    """
    return _user_directory(settings, WORKSPACES_DIRNAME, identity)


def session_directory(settings: Settings, identity: Identity) -> Path:
    """返回身份对应的会话文件目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        identity: 租户与用户身份。

    Returns:
        <data.root>/sessions/<tenant_id>/<user_id> 的绝对路径（不保证存在）。
    """
    return _user_directory(settings, SESSIONS_DIRNAME, identity)


def _user_directory(settings: Settings, dirname: str, identity: Identity) -> Path:
    """拼接数据根目录下某类数据的用户级目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        dirname: 数据类别子目录名。
        identity: 租户与用户身份。

    Returns:
        <data.root>/<dirname>/<tenant_id>/<user_id> 的绝对路径。
    """
    return (
        resolve_project_path(settings.data.root)
        / dirname
        / identity.tenant_id
        / identity.user_id
    )
