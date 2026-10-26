"""One process-owned, observable bulk job for the admin panel.

Either a re-extraction of every document or a re-planning of every CheckPlan; one lock
keeps them and the manual operations from running at the same time.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from src.dto.check_plan import CheckPlanReplanRequest

log = structlog.get_logger(__name__)

# Plans per replan page: large enough to keep the planner's concurrency busy.
REPLAN_PAGE = 100


class ReprocessingBusy(ValueError):
    pass


class BulkReprocessing:
    def __init__(self, repository, extraction, replanning=None):
        self.repository = repository
        self.extraction = extraction
        self.replanning = replanning
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
            "kind": "extraction",
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
            self._not_started()
            raise
        return self.status()

    async def start_replanning(self) -> dict:
        """Re-plan every automatic CheckPlan, the current planner version included.

        Restrictions keep their ids; reviewed and expert-authored plans stay as they are.
        """
        if self.replanning is None:
            raise RuntimeError("check plan re-planning is not configured")
        await self._acquire()
        self._status = {
            "id": str(uuid4()),
            "kind": "replan",
            "state": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "total": 0,
            "processed": 0,
            "written": 0,
            "failed": 0,
            "auto": 0,
            "unsupported": 0,
        }
        try:
            self._status["total"] = await self.replanning.count_replannable(
                include_current=True
            )
            self._task = asyncio.create_task(self._replan())
        except BaseException:
            self._not_started()
            raise
        return self.status()

    def _not_started(self):
        self._status["state"] = "failed"
        self._status["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._lock.release()

    async def _replan(self):
        try:
            after_id = None
            while True:
                page = await self.replanning.replan(
                    CheckPlanReplanRequest(
                        limit=REPLAN_PAGE,
                        after_id=after_id,
                        dry_run=False,
                        include_items=False,
                        include_current=True,
                    )
                )
                self._status["processed"] += page.selected
                self._status["written"] += page.written
                self._status["failed"] += page.failed
                for transition, count in page.transitions.items():
                    outcome = "auto" if transition.endswith("->auto") else "unsupported"
                    self._status[outcome] += count
                after_id = page.next_after_id
                if not page.has_more or not after_id:
                    break
            self._status["state"] = (
                "completed_with_errors" if self._status["failed"] else "completed"
            )
        except asyncio.CancelledError:
            self._status["state"] = "interrupted"
            raise
        except Exception:
            self._status["state"] = "failed"
            log.exception("admin_replanning_failed")
        finally:
            self._status["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._lock.release()
            log.info("admin_replanning_finished", **self.status())

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
