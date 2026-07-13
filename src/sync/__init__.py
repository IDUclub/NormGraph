"""Incremental DVD→graph sync: Kafka ``document.events`` consumer + startup reconcile."""

from src.sync.consumer import KafkaSyncConsumer
from src.sync.service import (
    DeleteResult,
    ReconcileResult,
    ScopeDeleteResult,
    SyncResult,
    SyncService,
)

__all__ = [
    "KafkaSyncConsumer",
    "SyncService",
    "SyncResult",
    "DeleteResult",
    "ScopeDeleteResult",
    "ReconcileResult",
]
