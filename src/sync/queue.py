"""Newest-first queue of pending DVD→graph syncs.

IDU_DVD announces changes on Kafka in the order they happened, and the startup reconcile walks the
whole library, so a backlog (a bulk reparse, a first boot) used to be worked off oldest first — a
document uploaded a minute ago waited behind hours of old ones. Both paths now only schedule work
here, and one worker always runs the most recently changed document next.

Jobs are keyed per document: a new event for a document that is still waiting is merged into its
job instead of queueing a second pass. A merged job applies its deletions before its sync, and the
sync reads IDU_DVD's current state, so the order of one document's own events still holds while
different documents are reordered.

The Kafka offset is committed as soon as an event is scheduled, so pending jobs are persisted as
``:SyncJob`` nodes and reloaded on start — a restart or redeploy must not lose them.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

# Longest pause between retries of a job whose sync keeps failing (IDU_DVD or Neo4j down).
_MAX_RETRY_DELAY = 900.0


def changed_at_from_iso(value: str | None) -> float:
    """Epoch seconds of an IDU_DVD ``uploaded_at``; documents without one sort as oldest."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


@dataclass
class SyncJob:
    key: str
    changed_at: float
    name: str | None = None
    user_id: str | None = None
    scenario_id: str | None = None
    sync: bool = False
    replace: bool = False
    delete_all: bool = False
    delete_versions: list[str] = field(default_factory=list)
    # Reconcile jobs target one doc_id; the action is decided when the job runs.
    doc_id: str | None = None
    content_hash: str | None = None
    version: str | None = None
    attempts: int = 0

    @classmethod
    def for_name(
        cls,
        name: str,
        *,
        changed_at: float,
        user_id: str | None = None,
        scenario_id: str | None = None,
        **actions,
    ) -> SyncJob:
        return cls(
            key=f"name:{user_id or ''}:{scenario_id or ''}:{name}",
            changed_at=changed_at,
            name=name,
            user_id=user_id,
            scenario_id=scenario_id,
            **actions,
        )

    @classmethod
    def for_document(
        cls,
        doc_id: str,
        *,
        changed_at: float,
        content_hash: str | None = None,
        version: str | None = None,
    ) -> SyncJob:
        return cls(
            key=f"doc:{doc_id}",
            changed_at=changed_at,
            doc_id=doc_id,
            content_hash=content_hash,
            version=version,
        )

    def merge(self, later: SyncJob) -> None:
        """Fold a later event for the same document into this pending job."""
        self.changed_at = max(self.changed_at, later.changed_at)
        if later.delete_all:
            # Nothing of the document is left, so what earlier events asked for is moot.
            self.delete_all, self.delete_versions = True, []
            self.sync = self.replace = False
        elif not self.delete_all:
            self.delete_versions = sorted(
                {*self.delete_versions, *later.delete_versions}
            )
        if later.sync:
            self.sync = True
            self.replace = self.replace or later.replace
        self.content_hash = later.content_hash or self.content_hash
        self.version = later.version or self.version

    def summary(self) -> dict:
        return {
            k: v for k, v in asdict(self).items() if v not in (None, False, [], 0)
        } | {"changed_at": self.changed_at}


class SyncRunner(Protocol):
    async def sync_name(self, name, *, user_id, scenario_id, replace): ...

    async def delete_name(
        self, name, *, user_id, scenario_id, versions, document_removed
    ): ...

    async def reconcile_document(self, doc_id, *, content_hash, version): ...


class SyncJobStore(Protocol):
    async def save_sync_job(self, props: dict) -> None: ...

    async def delete_sync_job(self, key: str) -> None: ...

    async def sync_jobs(self) -> list[dict]: ...


class SyncQueue:
    def __init__(
        self, sync: SyncRunner, store: SyncJobStore, *, retry_delay: float = 60.0
    ) -> None:
        self._sync = sync
        self._store = store
        self._retry_delay = retry_delay
        self._jobs: dict[str, SyncJob] = {}
        self._wakeup = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._retries: set[asyncio.Task] = set()
        self.running: SyncJob | None = None

    async def put(self, job: SyncJob) -> None:
        pending = self._jobs.get(job.key)
        if pending is None:
            self._jobs[job.key] = job
        else:
            pending.merge(job)
            job = pending
        await self._store.save_sync_job(asdict(job))
        self._wakeup.set()
        log.info("sync_job_queued", key=job.key, pending=len(self._jobs))

    def pending(self) -> list[SyncJob]:
        """Waiting jobs in the order they will run."""
        return sorted(self._jobs.values(), key=lambda j: j.changed_at, reverse=True)

    def status(self, limit: int = 20) -> dict:
        return {
            "pending": len(self._jobs),
            "retrying": len(self._retries),
            "running": self.running.summary() if self.running else None,
            "next": [job.summary() for job in self.pending()[:limit]],
        }

    async def start(self) -> None:
        for props in await self._store.sync_jobs():
            job = SyncJob(**props)
            if job.key in self._jobs:
                self._jobs[job.key].merge(job)
            else:
                self._jobs[job.key] = job
        if self._jobs:
            log.info("sync_jobs_restored", pending=len(self._jobs))
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in (self._worker, *self._retries) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = None

    def _next(self) -> SyncJob | None:
        if not self._jobs:
            return None
        key = max(self._jobs, key=lambda k: self._jobs[k].changed_at)
        return self._jobs.pop(key)

    async def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            try:
                await self._process(job)
            except Exception as exc:  # noqa: BLE001 — the worker must outlive a bad job
                log.error("sync_job_crashed", key=job.key, error=str(exc))

    async def _process(self, job: SyncJob) -> None:
        self.running = job
        try:
            await self._execute(job)
        except Exception as exc:  # noqa: BLE001 — retried below, never dropped
            job.attempts += 1
            log.warning(
                "sync_job_failed", key=job.key, attempt=job.attempts, error=str(exc)
            )
            self._retry_later(job)
            return
        finally:
            self.running = None
        if job.key in self._jobs:
            # A newer event arrived while this ran: keep its persisted job.
            await self._store.save_sync_job(asdict(self._jobs[job.key]))
        else:
            await self._store.delete_sync_job(job.key)

    async def _execute(self, job: SyncJob) -> None:
        if job.doc_id:
            await self._sync.reconcile_document(
                job.doc_id, content_hash=job.content_hash, version=job.version
            )
            return
        if job.delete_all or job.delete_versions:
            await self._sync.delete_name(
                job.name,
                user_id=job.user_id,
                scenario_id=job.scenario_id,
                versions=job.delete_versions,
                document_removed=job.delete_all,
            )
        if job.sync:
            await self._sync.sync_name(
                job.name,
                user_id=job.user_id,
                scenario_id=job.scenario_id,
                replace=job.replace,
            )

    def _retry_later(self, job: SyncJob) -> None:
        delay = min(self._retry_delay * 2 ** (job.attempts - 1), _MAX_RETRY_DELAY)

        async def _requeue() -> None:
            await asyncio.sleep(delay)
            await self.put(job)

        task = asyncio.create_task(_requeue())
        self._retries.add(task)
        task.add_done_callback(self._retries.discard)
