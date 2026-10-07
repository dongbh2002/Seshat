"""修改信号采集：把文档修订、批注、版本差异、建议结局与用户决定沉淀为统一的修改信号，供记忆沉淀使用。"""

from backend.signals.collector import FeedbackCollector
from backend.signals.comparison import SignalOrigin, VersionComparer
from backend.signals.document import REJECT_ALL, DocxVersion, fingerprint_similarity
from backend.signals.hooks import ReviewFeedbackHook, VersionObserverHook
from backend.signals.lineage import (
    REVISION_PREFIX_LENGTH,
    DocumentLineage,
    VersionRecord,
)
from backend.signals.models import (
    AUTHOR_ROLES,
    SIGNAL_OUTCOMES,
    SIGNAL_ROLES,
    SIGNAL_SOURCES,
    Signal,
)
from backend.signals.roster import AuthorRoster
from backend.signals.store import SignalStore
from backend.signals.transcript import TranscriptWriter, TurnCollector, TurnRecord

__all__ = [
    "AUTHOR_ROLES",
    "REJECT_ALL",
    "REVISION_PREFIX_LENGTH",
    "SIGNAL_OUTCOMES",
    "SIGNAL_ROLES",
    "SIGNAL_SOURCES",
    "AuthorRoster",
    "DocumentLineage",
    "DocxVersion",
    "FeedbackCollector",
    "ReviewFeedbackHook",
    "Signal",
    "SignalOrigin",
    "SignalStore",
    "TranscriptWriter",
    "TurnCollector",
    "TurnRecord",
    "VersionComparer",
    "VersionObserverHook",
    "VersionRecord",
    "fingerprint_similarity",
]
