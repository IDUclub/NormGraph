"""One process-owned, observable bulk extraction job for the admin panel."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import structlog

log = structlog.get_logger(__name__)


class ReprocessingBusy(ValueError):
    pass


class BulkReprocessing:
    def __init__(self, repository, extraction):
        self.repository = repository
        self.extraction = extraction
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._status = {"state": "idle"}

    def status(self) -> dict:
        return deepcopy(self._status)

    async def _acquire(self):
        if self._lock.locked():
            raise ReprocessingBusy(
                "Уже выполняется обработка. Дождитесь её завершения."
            )
        await self._lock.acquire()

    @asynccontextmanager
    async def single_operation(self):
        await self._acquire()
        try:
            yield
        finally:
            self._lock.release()

    async def start(self) -> dict:
        await self._acquire()
        self._status = {
            "id": str(uuid4()),
            "state": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "restrictions": 0,
            "current_document": None,
            "errors": [],
            "errors_truncated": False,
        }
        try:
            # Snapshot once: replacement can change the graph while we process it.
            documents = await self.repository.reprocessing_documents()
            self._status["total"] = len(documents)
            self._task = asyncio.create_task(self._run(documents))
        except BaseException:
            self._status["state"] = "failed"
            self._status["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._lock.release()
            raise
        return self.status()

    def _error(self, document: dict, message: str):
        if len(self._status["errors"]) < 100:
            self._status["errors"].append({**document, "message": message})
        else:
            self._status["errors_truncated"] = True

    async def _run(self, documents: list[dict]):
        try:
            for document in documents:
                self._status["current_document"] = document
                try:
                    result = await self.extraction.extract_document(
                        document["doc_id"], replace=True
                    )
                    self._status["restrictions"] += result.restrictions
                    if result.incomplete or result.warnings:
                        self._status["failed"] += 1
                        self._error(
                            document,
                            "Извлечение завершено с ошибками или предупреждениями. Проверьте карточку и логи.",
                        )
                    elif result.skipped:
                        self._status["skipped"] += 1
                        self._error(
                            document, "Документ пропущен: нет сохранённых пунктов."
                        )
                    else:
                        self._status["succeeded"] += 1
                except Exception:
                    self._status["failed"] += 1
                    self._error(document, "Ошибка обработки. Подробности в логах.")
                    log.exception(
                        "admin_reprocessing_document_failed", doc_id=document["doc_id"]
                    )
                self._status["processed"] += 1
            self._status["state"] = (
                "completed_with_errors"
                if self._status["failed"] or self._status["skipped"]
                else "completed"
            )
        except asyncio.CancelledError:
            self._status["state"] = "interrupted"
            raise
        finally:
            self._status["current_document"] = None
            self._status["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._lock.release()
            log.info("admin_reprocessing_finished", **self.status())

    async def aclose(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            # A task cancelled before its first turn cannot execute its finally block.
            if self._status["state"] == "running":
                self._status["state"] = "interrupted"
                self._status["finished_at"] = datetime.now(timezone.utc).isoformat()
                self._lock.release()
