"""Consumer handlers queue sync jobs; the event schema is frozen to the DVD contract."""

from __future__ import annotations

import json
import time

import pytest
from confluent_kafka import TIMESTAMP_CREATE_TIME, TIMESTAMP_NOT_AVAILABLE

from src.common.config import Settings
from src.sync.consumer import (
    DocumentDeletedHandler,
    DocumentProcessedHandler,
    DocumentUpdatedHandler,
    KafkaSyncConsumer,
    _event_time,
)
from src.sync.events import DocumentDeleted, DocumentProcessed, DocumentUpdated
from src.sync.queue import SyncJob


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[SyncJob] = []

    async def put(self, job):
        self.jobs.append(job)


class FakeMessage:
    def __init__(self, millis, kind=TIMESTAMP_CREATE_TIME) -> None:
        self._stamp = (kind, millis)

    def timestamp(self):
        return self._stamp


def _only(queue: RecordingQueue) -> dict:
    (job,) = queue.jobs
    return job.summary()


@pytest.mark.asyncio
async def test_processed_handler_queues_a_sync_without_replace():
    queue = RecordingQueue()
    await DocumentProcessedHandler(queue).handle(
        DocumentProcessed(document_name="A"), FakeMessage(1_700_000_000_000)
    )
    assert _only(queue) == {
        "key": "name:::A",
        "name": "A",
        "sync": True,
        "changed_at": 1_700_000_000.0,
    }


@pytest.mark.asyncio
async def test_processed_handler_forwards_user_scope():
    queue = RecordingQueue()
    await DocumentProcessedHandler(queue).handle(
        DocumentProcessed(document_name="A", user_id="u1", scenario_id="s1"), None
    )
    job = queue.jobs[0]
    assert (job.name, job.user_id, job.scenario_id, job.sync) == ("A", "u1", "s1", True)
    assert job.key == "name:u1:s1:A"


@pytest.mark.asyncio
async def test_updated_handler_queues_a_replacing_sync():
    queue = RecordingQueue()
    await DocumentUpdatedHandler(queue).handle(
        DocumentUpdated(document_name="A", version="2016"), None
    )
    job = queue.jobs[0]
    assert (job.sync, job.replace) == (True, True)


@pytest.mark.asyncio
async def test_deleted_handler_forwards_versions_and_flag():
    queue = RecordingQueue()
    await DocumentDeletedHandler(queue).handle(
        DocumentDeleted(
            document_name="A", versions_removed=["2011"], document_removed=False
        ),
        None,
    )
    job = queue.jobs[0]
    assert (job.sync, job.delete_all, job.delete_versions) == (False, False, ["2011"])


@pytest.mark.asyncio
async def test_deleted_handler_forwards_user_scope():
    queue = RecordingQueue()
    await DocumentDeletedHandler(queue).handle(
        DocumentDeleted(
            document_name="A",
            versions_removed=[],
            document_removed=True,
            user_id="u1",
            scenario_id="s1",
        ),
        None,
    )
    job = queue.jobs[0]
    assert (job.user_id, job.scenario_id, job.delete_all) == ("u1", "s1", True)


def test_event_without_a_broker_timestamp_counts_as_just_received():
    before = time.time()
    stamp = _event_time(FakeMessage(-1, kind=TIMESTAMP_NOT_AVAILABLE))
    assert before <= stamp <= time.time()


def test_handlers_infer_their_event_type():
    assert DocumentProcessedHandler(RecordingQueue()).event_type is DocumentProcessed
    assert DocumentUpdatedHandler(RecordingQueue()).event_type is DocumentUpdated
    assert DocumentDeletedHandler(RecordingQueue()).event_type is DocumentDeleted


def test_consumer_disabled_without_bootstrap_servers():
    consumer = KafkaSyncConsumer(
        RecordingQueue(), Settings(kafka_bootstrap_servers=None)
    )
    assert consumer.enabled is False


def test_consumer_enabled_with_bootstrap_servers():
    consumer = KafkaSyncConsumer(
        RecordingQueue(), Settings(kafka_bootstrap_servers="kafka:9092")
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
        'and versions of the document from the DVD API"},'
        '{"name":"user_id","type":["null","string"],"default":null,'
        '"doc":"owner of the user-scoped index this document was ingested into; '
        'null for the shared/regular document corpus"},'
        '{"name":"scenario_id","type":["null","string"],"default":null,'
        '"doc":"scenario the document belongs to, when part of a user-scoped index"}]}'
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
        'are retrievable from the DVD API by name + version"},'
        '{"name":"user_id","type":["null","string"],"default":null,'
        '"doc":"owner of the user-scoped index this document belongs to; '
        'null for the shared/regular document corpus"},'
        '{"name":"scenario_id","type":["null","string"],"default":null,'
        '"doc":"scenario the document belongs to, when part of a user-scoped index"}]}'
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
        '"doc":"true when no versions of the document remain in the store"},'
        '{"name":"user_id","type":["null","string"],"default":null,'
        '"doc":"owner of the user-scoped index this document belonged to; '
        'null for the shared/regular document corpus"},'
        '{"name":"scenario_id","type":["null","string"],"default":null,'
        '"doc":"scenario the document belonged to, when part of a user-scoped index"}]}'
    ),
}


@pytest.mark.parametrize("model, expected", list(_EXPECTED_SCHEMAS.items()))
def test_event_schema_is_frozen(model, expected):
    assert json.dumps(model.avro_schema(), separators=(",", ":")) == expected
