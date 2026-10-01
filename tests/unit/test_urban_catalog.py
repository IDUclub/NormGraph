import httpx

from src.pipeline.urban_catalog import ALL_ZONES, UrbanCatalogProvider
from tests.unit._catalog import CATALOG

PAYLOAD = {
    "/api/v1/service_types": [{"service_type_id": 7, "name": "Школа"}],
    "/api/v1/physical_object_types": [
        {"physical_object_type_id": 4, "name": "Жилой дом"},
        {"physical_object_type_id": 9, "name": "Парк"},
    ],
    "/api/v1/functional_zones_types": [
        {
            "functional_zone_type_id": 1,
            "name": "residential",
            "zone_nickname": "Жилая зона",
        }
    ],
}


def _transport(calls, *, fail=False):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if fail:
            return httpx.Response(503)
        return httpx.Response(200, json=PAYLOAD[request.url.path])

    return httpx.MockTransport(handler)


async def test_provider_loads_once_and_caches():
    calls = []
    provider = UrbanCatalogProvider(
        "http://urban/api", transport=_transport(calls), ttl_seconds=60
    )
    catalog = await provider.get()
    assert await provider.get() is catalog
    assert len(calls) == 3
    assert catalog.resolve("жилой  дом", "physical_object").name == "Жилой дом"
    assert catalog.resolve("Жилая зона", "functional_zone").name == "residential"
    assert catalog.resolve(ALL_ZONES, "functional_zone").name == ALL_ZONES
    assert catalog.resolve("Школа", "physical_object") is None


async def test_failed_load_leaves_the_catalog_unavailable():
    provider = UrbanCatalogProvider(
        "http://urban/api", transport=_transport([], fail=True)
    )
    assert await provider.get() is None


async def test_unconfigured_provider_is_unavailable():
    assert await UrbanCatalogProvider(None).get() is None


def test_ambiguous_untyped_name_is_unresolved():
    # «Парк» is both a service and a physical object in Urban API.
    assert CATALOG.resolve("Парк") is None
    assert CATALOG.resolve("Парк", "service").entity_type == "service"
    listing = CATALOG.prompt_listing()
    assert "residential (Жилая зона)" in listing and "Школа" in listing


def test_other_case_or_number_of_a_catalog_name_resolves_when_unique():
    from tests.unit._catalog import CATALOG

    assert CATALOG.resolve("Детские сады", "service").name == "Детский сад"
    assert CATALOG.resolve("школы").name == "Школа"
    assert CATALOG.resolve("Котельные установки", "physical_object") is None
