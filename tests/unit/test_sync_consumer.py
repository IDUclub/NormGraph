"""Consumer handlers dispatch to SyncService; the event schema is frozen to the DVD contract."""

from __future__ import annotations

import json

import pytest

from src.common.config import Settings
from src.sync.consumer import (
    DocumentDeletedHandler,
    DocumentProcessedHandler,
    DocumentUpdatedHandler,
    KafkaSyncConsumer,
)
from src.sync.events import DocumentDeleted, DocumentProcessed, DocumentUpdated


class RecordingSync:
    def __init__(self) -> None:
        self.synced: list[tuple[str, bool]] = []
        self.deleted: list[tuple[str, tuple, bool]] = []

    async def sync_name(self, name, *, replace=False):
        self.synced.append((name, replace))
        return []

    async def delete_name(self, name, *, versions=None, document_removed=True):
        self.deleted.append((name, tuple(versions or ()), document_removed))
        return None


@pytest.mark.asyncio
async def test_processed_handler_syncs_without_replace():
    sync = RecordingSync()
    await DocumentProcessedHandler(sync).handle(
        DocumentProcessed(document_name="A"), None
    )
    assert sync.synced == [("A", False)]


@pytest.mark.asyncio
async def test_updated_handler_syncs_with_replace():
    sync = RecordingSync()
    await DocumentUpdatedHandler(sync).handle(
        DocumentUpdated(document_name="A", version="2016"), None
    )
    assert sync.synced == [("A", True)]


@pytest.mark.asyncio
async def test_deleted_handler_forwards_versions_and_flag():
    sync = RecordingSync()
    await DocumentDeletedHandler(sync).handle(
        DocumentDeleted(
            document_name="A", versions_removed=["2011"], document_removed=False
        ),
        None,
    )
    assert sync.deleted == [("A", ("2011",), False)]


def test_handlers_infer_their_event_type():
    assert DocumentProcessedHandler(RecordingSync()).event_type is DocumentProcessed
    assert DocumentUpdatedHandler(RecordingSync()).event_type is DocumentUpdated
    assert DocumentDeletedHandler(RecordingSync()).event_type is DocumentDeleted


def test_consumer_disabled_without_bootstrap_servers():
    consumer = KafkaSyncConsumer(
        RecordingSync(), Settings(kafka_bootstrap_servers=None)
    )
    assert consumer.enabled is False


def test_consumer_enabled_with_bootstrap_servers():
    consumer = KafkaSyncConsumer(
        RecordingSync(), Settings(kafka_bootstrap_servers="kafka:9092")
    )
    assert consumer.enabled is True


# The Avro schema (record name, namespace, field docs) is the wire contract with IDU_DVD:
# otteroad matches consumed messages to these models by comparing the compact schema string
# with the writer schema from the registry. A change here silently drops events, so freeze it.
_EXPECTED_SCHEMAS = {
    DocumentProcessed: (
        '{"type":"record","name":"DocumentProcessed",'
        '"namespace":"document.events.documents",'
        '"doc":"Model for message indicates that a new document has been fully processed\\n'
        'and stored in the vector database for the first time.",'
        '"fields":[{"name":"document_name","type":"string",'
        '"doc":"unique document name (registry key), enough to fetch all fragments '
        'and versions of the document from the DVD API"}]}'
    ),
    DocumentUpdated: (
        '{"type":"record","name":"DocumentUpdated",'
        '"namespace":"document.events.documents",'
        '"doc":"Model for message indicates that a stored document changed in the vector\\n'
        "database: a new version was indexed (delta update) or the document was fully\\n"
        'reloaded from scratch.",'
        '"fields":[{"name":"document_name","type":"string",'
        '"doc":"unique document name (registry key) of the updated document"},'
        '{"name":"version","type":"string",'
        '"doc":"version tag the update was indexed under; fragments of this version '
        'are retrievable from the DVD API by name + version"}]}'
    ),
    DocumentDeleted: (
        '{"type":"record","name":"DocumentDeleted",'
        '"namespace":"document.events.documents",'
        '"doc":"Model for message indicates that a document (or one of its versions) was\\n'
        'removed from the vector database.",'
        '"fields":[{"name":"document_name","type":"string",'
        '"doc":"unique document name (registry key) the deletion applies to"},'
        '{"name":"versions_removed","type":{"type":"array","items":"string"},'
        '"doc":"version tags removed from the store by this deletion"},'
        '{"name":"document_removed","type":"boolean",'
        '"doc":"true when no versions of the document remain in the store"}]}'
    ),
}


@pytest.mark.parametrize("model, expected", list(_EXPECTED_SCHEMAS.items()))
def test_event_schema_is_frozen(model, expected):
    assert json.dumps(model.avro_schema(), separators=(",", ":")) == expected
