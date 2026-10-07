"""装配使用的路径：项目根目录、配置路径解析，以及运行时数据目录的布局。

数据根目录（yaml data.root）下的布局：
    workspaces/<tenant_id>/<user_id>/   待处理文档，工具只能访问这里
    sessions/<tenant_id>/<user_id>/     会话文件
    signals/<tenant_id>/<user_id>/      修改信号 signals.jsonl
    transcripts/<tenant_id>/<user_id>/  每个会话只追加的对话原文 <session_id>.jsonl
    snapshots/<tenant_id>/<user_id>/    见过的文档版本快照 <revision>.docx 与版本链 lineage.json
    memory/<tenant_id>/<user_id>/       用户级记忆 user.json、文档级记忆 documents/<paper_id>.json、提炼进度 state.json
    tenants/<tenant_id>/                课题组共享数据：成员表 roster.json、课题组级记忆 memory.json
    global/                             通用级记忆 memory.json（含待审核候选）
"""

from __future__ import annotations

from pathlib import Path

from backend.bootstrap.identity import Identity
from backend.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 项目根目录。
WORKSPACES_DIRNAME = "workspaces"  # 数据根目录下存放各用户工作区的子目录名。
SESSIONS_DIRNAME = "sessions"  # 数据根目录下存放各用户会话文件的子目录名。
SIGNALS_DIRNAME = "signals"  # 数据根目录下存放各用户修改信号的子目录名。
TRANSCRIPTS_DIRNAME = "transcripts"  # 数据根目录下存放各用户对话原文的子目录名。
SNAPSHOTS_DIRNAME = "snapshots"  # 数据根目录下存放各用户文档版本快照的子目录名。
TENANTS_DIRNAME = "tenants"  # 数据根目录下存放课题组共享数据的子目录名。
MEMORY_DIRNAME = "memory"  # 数据根目录下存放各用户记忆的子目录名。
GLOBAL_DIRNAME = "global"  # 数据根目录下存放通用数据的子目录名。


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


def signal_directory(settings: Settings, identity: Identity) -> Path:
    """返回身份对应的修改信号目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        identity: 租户与用户身份。

    Returns:
        <data.root>/signals/<tenant_id>/<user_id> 的绝对路径（不保证存在）。
    """
    return _user_directory(settings, SIGNALS_DIRNAME, identity)


def transcript_directory(settings: Settings, identity: Identity) -> Path:
    """返回身份对应的对话原文目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        identity: 租户与用户身份。

    Returns:
        <data.root>/transcripts/<tenant_id>/<user_id> 的绝对路径（不保证存在）。
    """
    return _user_directory(settings, TRANSCRIPTS_DIRNAME, identity)


def snapshot_directory(settings: Settings, identity: Identity) -> Path:
    """返回身份对应的文档版本快照目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        identity: 租户与用户身份。

    Returns:
        <data.root>/snapshots/<tenant_id>/<user_id> 的绝对路径（不保证存在）。
    """
    return _user_directory(settings, SNAPSHOTS_DIRNAME, identity)


def tenant_directory(settings: Settings, identity: Identity) -> Path:
    """返回身份所属课题组（租户）的共享数据目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        identity: 租户与用户身份。

    Returns:
        <data.root>/tenants/<tenant_id> 的绝对路径（不保证存在）。
    """
    return tenants_root(settings) / identity.tenant_id


def memory_root(settings: Settings) -> Path:
    """返回各用户记忆的根目录，其下按 <tenant_id>/<user_id> 分目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。

    Returns:
        <data.root>/memory 的绝对路径（不保证存在）。
    """
    return resolve_project_path(settings.data.root) / MEMORY_DIRNAME


def tenants_root(settings: Settings) -> Path:
    """返回各课题组共享数据的根目录，其下按 <tenant_id> 分目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。

    Returns:
        <data.root>/tenants 的绝对路径（不保证存在）。
    """
    return resolve_project_path(settings.data.root) / TENANTS_DIRNAME


def global_directory(settings: Settings) -> Path:
    """返回通用数据目录。

    Args:
        settings: 类型化项目配置，提供数据根目录。

    Returns:
        <data.root>/global 的绝对路径（不保证存在）。
    """
    return resolve_project_path(settings.data.root) / GLOBAL_DIRNAME


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
