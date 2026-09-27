"""启动身份解析：把启动参数中的租户与用户解析为运行身份（统一小写），都未提供时生成带时间码的游客身份。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from backend.config import Settings
from backend.utils.time_id import new_time_id

# 租户名与用户名允许的字符：字母、数字、中文、_、-；名字会拼进路径。
_NAME_PATTERN = re.compile(r"[\w-]+")


@dataclass(frozen=True)
class Identity:
    """一次启动使用的租户与用户身份。"""

    tenant_id: str  # 租户标识，通常对应课题组。
    user_id: str  # 用户标识。


def resolve_identity(
    settings: Settings,
    tenant_id: str | None,
    user_id: str | None,
) -> Identity:
    """解析启动身份：两者都提供时直接使用，都未提供时每次启动生成新的游客用户。

    Args:
        settings: 类型化项目配置，提供游客租户与游客用户名前缀。
        tenant_id: 启动参数中的租户名，未提供为 None。
        user_id: 启动参数中的用户名，未提供为 None。

    Returns:
        通过名字校验、租户名与用户名统一为小写的身份。

    Raises:
        ValueError: 只提供了其中一个，或名字含不允许的字符。
    """
    if tenant_id is None and user_id is None:
        # TODO: 记忆沉淀接入后排除游客数据；游客工作区与会话随启动次数累积，需要清理机制。
        guest = settings.identity
        tenant_id = guest.guest_tenant
        user_id = f"{guest.guest_user_prefix}-{new_time_id(datetime.now())}"
    elif tenant_id is None or user_id is None:
        raise ValueError("租户与用户须同时提供；都不提供时以游客身份启动")

    for label, name in (("租户", tenant_id), ("用户", user_id)):
        if not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"{label}名只能包含字母、数字、中文、_ 和 -: {name!r}")
    # 名字会拼进路径，而 Windows 路径不区分大小写；统一小写，保证一个目录只对应一个身份。
    return Identity(tenant_id=tenant_id.lower(), user_id=user_id.lower())
