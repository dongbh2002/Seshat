"""时间码 ID：创建时间 YYYYMMDD-HHMMSS 加 6 位随机十六进制，按字典序即按时间排序；会话 ID 与游客用户名共用。"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import uuid4

TIME_ID_PATTERN = re.compile(  # 时间码 ID 的完整格式，校验时应使用 fullmatch。
    r"\d{8}-\d{6}-[0-9a-f]{6}"
)


def new_time_id(moment: datetime) -> str:
    """生成以指定时间开头的时间码 ID，随机后缀避免同一秒内重复。

    Args:
        moment: ID 中记录的时间。

    Returns:
        形如 ``20260927-213000-a1b2c3`` 的 ID。
    """
    return f"{moment:%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
