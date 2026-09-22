"""Bounded extraction recovery for ingested documents without restrictions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractionBackfillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=1, ge=1, le=20)
    after_id: str | None = Field(default=None, min_length=1, max_length=128)
    dry_run: bool = False


class ExtractionBackfillItem(BaseModel):
    doc_id: str
    status: Literal["selected", "extracted", "skipped", "failed"]
    clauses_processed: int = 0
    restrictions: int = 0
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    failed_clause_ids: list[str] = Field(default_factory=list)


class ExtractionBackfillResponse(BaseModel):
    selected: int
    extracted: int = 0
    skipped: int = 0
    failed: int = 0
    restrictions: int = 0
    items: list[ExtractionBackfillItem] = Field(default_factory=list)
    has_more: bool = False
    next_after_id: str | None = None
    dry_run: bool = False
