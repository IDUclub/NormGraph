"""Ingestion of IDU_DVD documents into the restriction graph."""

from src.ingestion.service import IngestionService, IngestResult  # noqa: F401

__all__ = ["IngestionService", "IngestResult"]
