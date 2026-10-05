"""Neo4j storage of zone regulations: one ``:Zone`` per zone of a ПЗЗ document.

The regulation itself (uses, parameters, notes) is kept as JSON on the node; the ВРИ codes of
each section are also stored as lists, so «zones where 2.1.1 is permitted» is a plain filter.
"""

from __future__ import annotations

import json

from src.graph.client import Neo4jClient
from src.regulations.parser import SECTIONS, ZoneRegulation


def _codes(reg: ZoneRegulation, section: str) -> list[str]:
    return sorted({code for use in reg.uses if use.section == section for code in use.codes})


class ZoneStore:
    def __init__(self, client: Neo4jClient) -> None:
        self.client = client

    async def replace(self, doc_id: str, zones: list[ZoneRegulation]) -> int:
        """Replace the zones of a document with ``zones`` (none removes them)."""
        rows = [
            {
                "key": f"{doc_id}:{reg.code}",
                "code": reg.code,
                "name": reg.name,
                "article": reg.article,
                "group": reg.group,
                "regulation": json.dumps(reg.to_dict(), ensure_ascii=False),
                **{f"{section}_codes": _codes(reg, section) for section in SECTIONS},
                "order": index,
            }
            for index, reg in enumerate(zones)
        ]
        await self.client.run(
            """
            MATCH (z:Zone)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})
            DETACH DELETE z
            """,
            doc_id=doc_id,
        )
        if not rows:
            return 0
        created = await self.client.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            UNWIND $rows AS row
            CREATE (z:Zone)
            SET z = row, z.doc_id = $doc_id
            CREATE (z)-[:IN_DOCUMENT]->(d)
            RETURN count(z) AS zones
            """,
            doc_id=doc_id,
            rows=rows,
        )
        return created[0]["zones"] if created else 0

    async def zones(
        self,
        *,
        territory_ids: list[int] | None = None,
        doc_id: str | None = None,
        codes: list[str] | None = None,
        vri_code: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Zones of the shared corpus with their document, in document order."""
        return await self.client.run(
            """
            MATCH (z:Zone)-[:IN_DOCUMENT]->(d:Document)
            WHERE d.user_id IS NULL
              AND ($doc_id IS NULL OR d.doc_id = $doc_id)
              AND ($territory_ids IS NULL OR d.territory_id IN $territory_ids)
              AND ($codes IS NULL OR z.code IN $codes)
              AND ($vri IS NULL OR $vri IN z.main_codes OR $vri IN z.conditional_codes
                   OR $vri IN z.auxiliary_codes)
            RETURN z.regulation AS regulation, d.doc_id AS doc_id, d.name AS name,
                   d.title AS title, d.version AS version,
                   d.territory_id AS territory_id, d.territory_name AS territory_name,
                   d.effective_date AS effective_date
            ORDER BY d.name, z.order
            LIMIT $limit
            """,
            territory_ids=territory_ids or None,
            doc_id=doc_id,
            codes=codes or None,
            vri=vri_code,
            limit=limit,
        )

    async def documents(self, territory_ids: list[int] | None = None) -> list[dict]:
        """Documents with zone regulations and how many zones each has."""
        return await self.client.run(
            """
            MATCH (z:Zone)-[:IN_DOCUMENT]->(d:Document)
            WHERE d.user_id IS NULL
              AND ($territory_ids IS NULL OR d.territory_id IN $territory_ids)
            RETURN d.doc_id AS doc_id, d.name AS name, d.title AS title,
                   d.version AS version, d.territory_id AS territory_id,
                   d.territory_name AS territory_name,
                   d.effective_date AS effective_date, count(z) AS zones
            ORDER BY d.name
            """,
            territory_ids=territory_ids or None,
        )
