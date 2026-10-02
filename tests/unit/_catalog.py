"""A small Urban API catalog snapshot for planner tests."""

from src.pipeline.urban_catalog import StaticCatalogProvider, UrbanCatalog

CATALOG = UrbanCatalog.from_payload(
    {
        "service_types": [
            {"name": name}
            for name in ("Школа", "Детский сад", "Поликлиника", "Парк", "Аптека")
        ],
        "physical_object_types": [
            {"name": name}
            for name in (
                "Жилой дом",
                "Нежилое здание",
                "Парк",
                "Автозаправочная станция",
            )
        ],
        "functional_zones_types": [
            {"name": "residential", "zone_nickname": "Жилая зона"},
            {"name": "industrial", "zone_nickname": "Промышленная зона"},
        ],
    }
)


def catalog_provider() -> StaticCatalogProvider:
    return StaticCatalogProvider(CATALOG)
