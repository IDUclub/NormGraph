"""Composition root: build and share the process-wide dependencies.

A single ``Dependencies`` instance is created in the FastAPI lifespan (see ``src/main.py``) and
reused by routers and the MCP tools via ``get_dependencies()``. Keeping construction in one place
makes the external boundaries (Neo4j, the LLM, the embedder) easy to see and to swap in tests.
"""

from __future__ import annotations

import structlog
from idu_service_auth import KeycloakTokenClient

from src.admin_service.repository import AdminRepository
from src.admin_service.reprocessing import BulkReprocessing
from src.common.auth import build_service_auth
from src.common.config import Settings, settings
from src.common.logger import configure_logging
from src.dvd_client import DVDClient
from src.graph import Neo4jClient
from src.graph.reader import GraphReader
from src.graph.writer import GraphWriter
from src.ingestion import IngestionService
from src.pipeline.check_plan_backfill import CheckPlanBackfillService
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.extractor import RestrictionExtractor
from src.pipeline.restriction_reembed import RestrictionReembedService
from src.pipeline.service import ExtractionService
from src.pipeline.urban_catalog import UrbanCatalogProvider
from src.pipeline.vocabulary import EntityResolver, KindVocabulary
from src.providers import Embedder, LLMProvider, build_embedder, build_llm
from src.providers.langextract_backend import ProviderLanguageModel
from src.query import QueryService
from src.sync import KafkaSyncConsumer, SyncQueue, SyncService

log = structlog.get_logger(__name__)


class Dependencies:
    def __init__(
        self,
        settings: Settings,
        service_auth: KeycloakTokenClient,
        graph: Neo4jClient,
        llm: LLMProvider,
        embedder: Embedder,
        dvd: DVDClient,
        writer: GraphWriter,
        ingestion: IngestionService,
        kinds: KindVocabulary,
        extraction: ExtractionService,
        check_plan_backfill: CheckPlanBackfillService,
        restriction_reembed: RestrictionReembedService,
        query: QueryService,
        sync: SyncService,
        sync_queue: SyncQueue,
        consumer: KafkaSyncConsumer,
    ) -> None:
        self.settings = settings
        self.service_auth = service_auth
        self.graph = graph
        self.llm = llm
        self.embedder = embedder
        self.dvd = dvd
        self.writer = writer
        self.ingestion = ingestion
        self.kinds = kinds
        self.extraction = extraction
        self.check_plan_backfill = check_plan_backfill
        self.restriction_reembed = restriction_reembed
        self.query = query
        self.sync = sync
        self.sync_queue = sync_queue
        self.consumer = consumer
        self.bulk_reprocessing = BulkReprocessing(
            AdminRepository(graph), extraction, replanning=check_plan_backfill
        )

    async def aclose(self) -> None:
        await self.bulk_reprocessing.aclose()
        await self.graph.close()
        await self.llm.aclose()
        await self.embedder.aclose()
        await self.dvd.aclose()


_deps: Dependencies | None = None


def init_dependencies() -> Dependencies:
    """Build the shared dependencies (idempotent within a process)."""
    global _deps
    configure_logging(settings)
    service_auth = build_service_auth(settings)
    graph = Neo4jClient(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        database=settings.neo4j_database,
    )
    writer = GraphWriter(graph)
    dvd = DVDClient(
        settings.dvd_base_url,
        service_auth,
        timeout=settings.dvd_timeout,
    )
    # Reference back-fill via /search is a stopgap until IDU_DVD's library API surfaces
    # DocumentFragment.references; keep it off by default (one search per clause).
    ingestion = IngestionService(dvd, writer, backfill_references=False)

    llm = build_llm(settings)
    embedder = build_embedder(settings)
    lx_model = ProviderLanguageModel(
        llm, model_id=settings.llm_model, temperature=settings.llm_temperature
    )
    extractor = RestrictionExtractor(
        lx_model, extraction_passes=settings.extraction_passes
    )
    kinds = KindVocabulary(
        writer,
        embedder,
        threshold=settings.kind_match_threshold,
        index=settings.kind_vector_index,
    )
    entities = EntityResolver(
        writer,
        embedder,
        threshold=settings.entity_merge_threshold,
        index=settings.entity_vector_index,
    )
    check_plan_planner = CheckPlanPlanner(
        llm,
        catalog=UrbanCatalogProvider(
            settings.urban_api_url, ttl_seconds=settings.urban_catalog_ttl_seconds
        ),
        refine=settings.check_plan_rewrite,
        verify=settings.check_plan_verify,
        votes=settings.check_plan_rewrite_votes,
        agreement=settings.check_plan_rewrite_agreement,
        min_distance_m=settings.check_plan_min_distance_m,
        llm_concurrency=settings.check_plan_llm_concurrency,
        reasoning_effort=settings.check_plan_reasoning_effort,
        transport_speed_kmh=settings.check_plan_transport_speed_kmh,
    )
    extraction = ExtractionService(
        writer,
        extractor,
        kinds,
        entities,
        embedder,
        extract_concurrency=settings.extract_concurrency,
        check_plan_planner=check_plan_planner,
    )

    reader = GraphReader(graph)
    check_plan_backfill = CheckPlanBackfillService(
        reader,
        writer,
        check_plan_planner,
        concurrency=settings.extract_concurrency,
    )
    restriction_reembed = RestrictionReembedService(
        reader, writer, embedder, batch=settings.embed_batch
    )
    query = QueryService(reader, embedder, dvd, settings, writer=writer)

    sync = SyncService(dvd, writer, ingestion, extraction)
    sync_queue = SyncQueue(sync, writer)
    sync.queue = sync_queue
    consumer = KafkaSyncConsumer(sync_queue, settings)

    _deps = Dependencies(
        settings=settings,
        service_auth=service_auth,
        graph=graph,
        llm=llm,
        embedder=embedder,
        dvd=dvd,
        writer=writer,
        ingestion=ingestion,
        kinds=kinds,
        extraction=extraction,
        check_plan_backfill=check_plan_backfill,
        restriction_reembed=restriction_reembed,
        query=query,
        sync=sync,
        sync_queue=sync_queue,
        consumer=consumer,
    )
    log.info("dependencies_initialized", config=repr(settings))
    return _deps


def get_dependencies() -> Dependencies:
    """Return the shared dependencies; raise if the app has not initialised yet."""
    if _deps is None:
        raise RuntimeError("dependencies are not initialised")
    return _deps
