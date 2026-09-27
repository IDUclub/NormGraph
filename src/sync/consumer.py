"""Kafka consumer for IDU_DVD ``document.events`` (otteroad).

Runs inside the FastAPI lifespan and schedules each lifecycle event on the :class:`SyncQueue`,
stamped with the event's broker timestamp so the most recent change in IDU_DVD runs first:

* ``DocumentProcessed`` — a new document → ingest + extract it;
* ``DocumentUpdated``   — a document changed → re-ingest + re-extract incrementally (``replace``);
* ``DocumentDeleted``   — a document/version was removed → drop it from the graph.

A handler returns once the job is queued (and persisted), so otteroad commits the offset and keeps
reading — the backlog stays visible to the queue instead of waiting in the partition.

Consumption stays off until ``NG_KAFKA_BOOTSTRAP_SERVERS`` is set, so local setups without a broker
run unchanged (the startup reconcile still keeps the graph in step with IDU_DVD).
"""

from __future__ import annotations

import time

import structlog
from confluent_kafka import TIMESTAMP_NOT_AVAILABLE, Message
from otteroad import (
    BaseMessageHandler,
    KafkaConsumerService,
    KafkaConsumerSettings,
)

from src.common.config import Settings
from src.sync.events import DocumentDeleted, DocumentProcessed, DocumentUpdated
from src.sync.queue import SyncJob, SyncQueue
from src.sync.schema_compat import install_tolerant_schema_matching

log = structlog.get_logger(__name__)


def _event_time(ctx: Message | None) -> float:
    """When IDU_DVD published the event (epoch seconds); now if the broker gave no time."""
    if ctx is None:
        return time.time()
    kind, millis = ctx.timestamp()
    if kind == TIMESTAMP_NOT_AVAILABLE or millis <= 0:
        return time.time()
    return millis / 1000


class DocumentProcessedHandler(BaseMessageHandler[DocumentProcessed]):
    """New document indexed in IDU_DVD → ingest + extract it into the graph."""

    def __init__(self, queue: SyncQueue) -> None:
        super().__init__()
        self._queue = queue

    async def on_startup(self) -> None:  # pragma: no cover - lifecycle hook
        return None

    async def on_shutdown(self) -> None:  # pragma: no cover - lifecycle hook
        return None

    async def handle(self, event: DocumentProcessed, ctx: Message) -> None:
        log.info(
            "event_document_processed",
            document_name=event.document_name,
            user_id=event.user_id,
            scenario_id=event.scenario_id,
        )
        await self._queue.put(
            SyncJob.for_name(
                event.document_name,
                changed_at=_event_time(ctx),
                user_id=event.user_id,
                scenario_id=event.scenario_id,
                sync=True,
            )
        )


class DocumentUpdatedHandler(BaseMessageHandler[DocumentUpdated]):
    """Document changed in IDU_DVD → re-ingest + re-extract incrementally (replace)."""

    def __init__(self, queue: SyncQueue) -> None:
        super().__init__()
        self._queue = queue

    async def on_startup(self) -> None:  # pragma: no cover - lifecycle hook
        return None

    async def on_shutdown(self) -> None:  # pragma: no cover - lifecycle hook
        return None

    async def handle(self, event: DocumentUpdated, ctx: Message) -> None:
        log.info(
            "event_document_updated",
            document_name=event.document_name,
            version=event.version,
            user_id=event.user_id,
            scenario_id=event.scenario_id,
        )
        await self._queue.put(
            SyncJob.for_name(
                event.document_name,
                changed_at=_event_time(ctx),
                user_id=event.user_id,
                scenario_id=event.scenario_id,
                sync=True,
                replace=True,
            )
        )


class DocumentDeletedHandler(BaseMessageHandler[DocumentDeleted]):
    """Document (or a version of it) removed from IDU_DVD → drop it from the graph."""

    def __init__(self, queue: SyncQueue) -> None:
        super().__init__()
        self._queue = queue

    async def on_startup(self) -> None:  # pragma: no cover - lifecycle hook
        return None

    async def on_shutdown(self) -> None:  # pragma: no cover - lifecycle hook
        return None

    async def handle(self, event: DocumentDeleted, ctx: Message) -> None:
        log.info(
            "event_document_deleted",
            document_name=event.document_name,
            versions_removed=event.versions_removed,
            document_removed=event.document_removed,
            user_id=event.user_id,
            scenario_id=event.scenario_id,
        )
        await self._queue.put(
            SyncJob.for_name(
                event.document_name,
                changed_at=_event_time(ctx),
                user_id=event.user_id,
                scenario_id=event.scenario_id,
                delete_all=event.document_removed,
                delete_versions=(
                    [] if event.document_removed else list(event.versions_removed)
                ),
            )
        )


class KafkaSyncConsumer:
    """Owns the otteroad consumer service; a no-op when Kafka is not configured."""

    def __init__(self, queue: SyncQueue, settings: Settings) -> None:
        self._queue = queue
        self._settings = settings
        self._service: KafkaConsumerService | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(enabled={self.enabled}, "
            f"servers={self._settings.kafka_bootstrap_servers})"
        )

    @property
    def enabled(self) -> bool:
        return bool(self._settings.kafka_bootstrap_servers)

    async def start(self) -> None:
        """Create the consumer, register the handlers and start the worker (no-op if disabled)."""
        if not self.enabled:
            log.info("kafka_consumer_disabled")
            return
        # The contour Schema Registry stores DocumentProcessed with a doc/default key order the
        # current otteroad doesn't reproduce; make model resolution order-insensitive so events
        # are not silently dropped (see src/sync/schema_compat.py).
        install_tolerant_schema_matching()
        consumer_settings = KafkaConsumerSettings(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            client_id=self._settings.kafka_client_id,
            group_id=self._settings.kafka_group_id,
            schema_registry_url=self._settings.kafka_schema_registry_url,
            auto_offset_reset=self._settings.kafka_auto_offset_reset,
        )
        service = KafkaConsumerService(consumer_settings, logger=log)
        for handler in (
            DocumentProcessedHandler(self._queue),
            DocumentUpdatedHandler(self._queue),
            DocumentDeletedHandler(self._queue),
        ):
            service.register_handler(handler)
        service.add_worker(topics=self._settings.kafka_topic)
        await service.start()
        self._service = service
        log.info(
            "kafka_consumer_started",
            servers=self._settings.kafka_bootstrap_servers,
            topic=self._settings.kafka_topic,
            group_id=self._settings.kafka_group_id,
        )

    async def stop(self) -> None:
        if self._service is not None:
            await self._service.stop()
            self._service = None
        log.info("kafka_consumer_stopped")
