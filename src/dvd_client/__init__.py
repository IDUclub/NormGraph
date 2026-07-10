"""Client for the IDU_DVD service (source of documents, clauses and references)."""

from src.dvd_client.client import DVDClient  # noqa: F401
from src.dvd_client.models import (  # noqa: F401
    DocumentDetail,
    DocumentFragment,
    DocumentRef,
    DocumentSummary,
    SearchHit,
    SearchResponse,
)

__all__ = [
    "DVDClient",
    "DocumentDetail",
    "DocumentFragment",
    "DocumentRef",
    "DocumentSummary",
    "SearchHit",
    "SearchResponse",
]
