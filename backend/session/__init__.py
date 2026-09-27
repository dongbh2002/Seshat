"""会话包：随会话存在的结构化工作状态及其写入 Hook，以及会话的持久化与管理。"""

from backend.session.manager import SessionManager, SessionStateful
from backend.session.repository import SCHEMA_VERSION, SessionRecord, SessionRepository
from backend.session.review_state import (
    FINDING_STATUSES,
    ReviewFinding,
    ReviewStateStore,
)
from backend.session.review_state_hook import ReviewStateHook

__all__ = [
    "FINDING_STATUSES",
    "SCHEMA_VERSION",
    "ReviewFinding",
    "ReviewStateHook",
    "ReviewStateStore",
    "SessionManager",
    "SessionRecord",
    "SessionRepository",
    "SessionStateful",
]
