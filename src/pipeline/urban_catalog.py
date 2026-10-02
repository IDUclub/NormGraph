"""Canonical Urban API type dictionaries for grounding CheckPlan layer entities.

The compliance executor resolves every layer ``entity`` against the same global
dictionaries, so a plan naming anything else can never run. Plans are therefore
grounded here, while they are built: an entity either is a canonical type name
or the plan is not executable.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from time import monotonic
from typing import Literal

import httpx
import structlog

from src.pipeline.catalog_aliases import alias_names

log = structlog.get_logger(__name__)

EntityType = Literal["service", "physical_object", "functional_zone"]

# Every functional zone of the scenario, whatever its type.
ALL_ZONES = "functional_zones"


def normalize_name(value: str) -> str:
    folded = value.casefold().replace("ё", "е")
    return " ".join(re.sub(r"[\"'«»“”„()\[\].,;:]", " ", folded).split())


_ENDING = re.compile(
    r"(?:ами|ями|ого|его|ому|ему|ыми|ими|ых|их|ой|ей|ий|ый|ая|яя|ое|ее|ые|ие|"
    r"ов|ев|ам|ям|ах|ях|ом|ем|ью|ия|ья|ь|а|я|о|е|ы|и|у|ю)$"
)


def stem_name(value: str) -> str:
    """A normalized name with Russian case and number endings cut off each word."""
    return " ".join(
        _ENDING.sub("", word) if len(word) > 3 else word
        for word in normalize_name(value).split()
    )


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    entity_type: EntityType
    # Human label shown to the model (zones are keyed by their code name).
    label: str


class UrbanCatalog:
    """Immutable snapshot of the service, physical-object and functional-zone types."""

    def __init__(self, entries: list[CatalogEntry]) -> None:
        self.entries = entries
        self._by_key: dict[tuple[str, str], CatalogEntry] = {}
        self._by_name: dict[str, list[CatalogEntry]] = {}
        self._by_stem: dict[str, list[CatalogEntry]] = {}
        for entry in entries:
            for key in {normalize_name(entry.name), normalize_name(entry.label)}:
                self._by_key.setdefault((entry.entity_type, key), entry)
                self._by_name.setdefault(key, []).append(entry)
            for key in {stem_name(entry.name), stem_name(entry.label)}:
                if entry not in self._by_stem.setdefault(key, []):
                    self._by_stem[key].append(entry)

    @classmethod
    def from_payload(cls, payload: dict[str, list[dict]]) -> "UrbanCatalog":
        entries = [
            CatalogEntry(row["name"].strip(), "service", row["name"].strip())
            for row in payload.get("service_types", [])
        ]
        entries += [
            CatalogEntry(row["name"].strip(), "physical_object", row["name"].strip())
            for row in payload.get("physical_object_types", [])
        ]
        entries += [
            CatalogEntry(
                row["name"].strip(),
                "functional_zone",
                (row.get("zone_nickname") or row["name"]).strip(),
            )
            for row in payload.get("functional_zones_types", [])
        ]
        entries.append(CatalogEntry(ALL_ZONES, "functional_zone", "любая зона"))
        return cls(entries)

    def resolve(
        self, name: str, entity_type: EntityType | None = None
    ) -> CatalogEntry | None:
        """The canonical entry for an exact (normalized) name, optionally of one type.

        An untyped name that exists as both a service and a physical object (a park,
        a petrol station) is ambiguous and stays unresolved.
        """
        key = normalize_name(name)
        if entity_type is not None:
            entry = self._by_key.get((entity_type, key))
        else:
            matches = self._by_name.get(key, [])
            entry = matches[0] if len(matches) == 1 else None
        if entry is not None or key in self._by_name:
            return entry
        # Another case or number of a catalog name («Детские лагеря» → «Детский
        # лагерь»), only when it points to exactly one entry.
        matches = [
            item
            for item in self._by_stem.get(stem_name(name), [])
            if entity_type is None or item.entity_type == entity_type
        ]
        if matches:
            return matches[0] if len(matches) == 1 else None
        # A norm's own wording of a type («общеобразовательные организации» →
        # «Школа»), only when it names exactly one catalog entry.
        aliased = {
            entry.name: entry
            for alias in alias_names(key)
            for entry in self._by_name.get(normalize_name(alias), [])
            if entity_type is None or entry.entity_type == entity_type
        }
        entries = list(aliased.values())
        if entity_type is None and len(entries) == 1:
            # The same name under both object types stays ambiguous.
            same = self._by_name.get(normalize_name(entries[0].name), [])
            return entries[0] if len(same) == 1 else None
        return entries[0] if len(entries) == 1 else None

    def prompt_listing(self) -> str:
        """Compact listing of every canonical name for an LLM prompt."""
        sections = {
            "service": "Сервисы (entity_type=service)",
            "physical_object": "Физические объекты (entity_type=physical_object)",
            "functional_zone": "Функциональные зоны (entity_type=functional_zone), "
            "указывай код до скобок",
        }
        lines = []
        for entity_type, title in sections.items():
            names = [
                (
                    f"{entry.name} ({entry.label})"
                    if entry.label != entry.name
                    else entry.name
                )
                for entry in self.entries
                if entry.entity_type == entity_type
            ]
            lines.append(f"{title}: " + "; ".join(names))
        return "\n".join(lines)


class UrbanCatalogProvider:
    """Load the dictionaries once and refresh them after ``ttl_seconds``.

    A failed refresh keeps the previous snapshot; without any snapshot the catalog
    is unavailable and LLM-built plans cannot be grounded.
    """

    _ENDPOINTS = ("service_types", "physical_object_types", "functional_zones_types")

    def __init__(
        self,
        base_url: str | None,
        *,
        ttl_seconds: float = 3600.0,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/" if base_url else None
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self.transport = transport
        self._catalog: UrbanCatalog | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> UrbanCatalog | None:
        if self.base_url is None:
            return None
        if self._catalog is not None and monotonic() < self._expires_at:
            return self._catalog
        async with self._lock:
            if self._catalog is not None and monotonic() < self._expires_at:
                return self._catalog
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout,
                    transport=self.transport,
                ) as client:
                    payload = {}
                    for endpoint in self._ENDPOINTS:
                        response = await client.get(f"v1/{endpoint}")
                        response.raise_for_status()
                        payload[endpoint] = response.json()
                self._catalog = UrbanCatalog.from_payload(payload)
                log.info(
                    "urban_catalog_loaded",
                    **{key: len(value) for key, value in payload.items()},
                )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("urban_catalog_unavailable", error=str(exc))
            # Retry a failed load no sooner than a minute later.
            self._expires_at = monotonic() + (
                self.ttl_seconds if self._catalog is not None else 60.0
            )
            return self._catalog


class StaticCatalogProvider:
    """A fixed catalog (tests, offline evaluation)."""

    def __init__(self, catalog: UrbanCatalog | None) -> None:
        self._catalog = catalog

    async def get(self) -> UrbanCatalog | None:
        return self._catalog
