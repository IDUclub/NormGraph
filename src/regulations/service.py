"""Zone regulations of ПЗЗ documents: built on ingest, served to PZZ compliance checks."""

from __future__ import annotations

import json
import re

import structlog

from src.dto.regulations import (
    RegulationDocument,
    ZoneListResponse,
    ZoneRegulationOut,
    ZoneSource,
)
from src.dvd_client.models import DocumentDetail
from src.regulations.parser import parse_regulations
from src.regulations.store import ZoneStore

log = structlog.get_logger(__name__)

# A document is read for zone regulations when it is land-use and development rules ...
_PZZ = re.compile(r"землепользовани[яе]\s+и\s+застройк|\bпзз\b", re.I)
# ... and has at least this many zones (a mention of a zone is not a regulation).
MIN_ZONES = 2


def is_pzz(detail: DocumentDetail) -> bool:
    if detail.amends or detail.explains:
        return False  # an act about the rules, not the rules
    return bool(_PZZ.search(f"{detail.name} {detail.title or ''}"))


class RegulationService:
    def __init__(self, store: ZoneStore) -> None:
        self.store = store

    async def rebuild(self, detail: DocumentDetail) -> int:
        """Read the zone regulations of a document (none for a document that is not a ПЗЗ)."""
        zones = parse_regulations(detail.fragments) if is_pzz(detail) else []
        if len(zones) < MIN_ZONES:
            zones = []
        count = await self.store.replace(detail.doc_id, zones)
        if count:
            log.info(
                "zone_regulations_built",
                doc_id=detail.doc_id,
                name=detail.name,
                zones=count,
                parameters=sum(len(z.parameters) for z in zones),
            )
        return count

    async def zones(
        self,
        *,
        territory_ids: list[int] | None = None,
        doc_id: str | None = None,
        codes: list[str] | None = None,
        vri_code: str | None = None,
        limit: int = 500,
    ) -> ZoneListResponse:
        rows = await self.store.zones(
            territory_ids=territory_ids,
            doc_id=doc_id,
            codes=codes,
            vri_code=vri_code,
            limit=limit,
        )
        zones = [
            ZoneRegulationOut(
                **json.loads(row["regulation"]),
                document=ZoneSource(
                    **{k: row[k] for k in ZoneSource.model_fields if k in row}
                ),
            )
            for row in rows
        ]
        return ZoneListResponse(count=len(zones), zones=zones)

    async def documents(
        self, territory_ids: list[int] | None = None
    ) -> list[RegulationDocument]:
        return [
            RegulationDocument(**row) for row in await self.store.documents(territory_ids)
        ]
