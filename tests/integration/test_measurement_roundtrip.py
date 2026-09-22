"""Measurement survives Neo4j persistence and targeted plan regeneration."""

import uuid

import pytest

from src.common.config import settings
from src.dto.check_plan import CheckPlanRegenerateRequest
from src.graph import Neo4jClient
from src.graph.reader import GraphReader
from src.graph.writer import GraphWriter
from src.pipeline.check_plan_backfill import CheckPlanBackfillService
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.models import RestrictionMeasurement


@pytest.mark.integration
async def test_measurement_roundtrip_and_regeneration():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:
        await client.close()
        pytest.skip(f"Neo4j unavailable: {exc}")
    rid = f"test-measurement-{uuid.uuid4().hex}"
    measurement = RestrictionMeasurement(
        kind="area_share",
        indicator="доля площади озеленения",
        basis="площади микрорайона",
        numerator_entity="озелененная территория",
        denominator_entity="микрорайон",
    )
    try:
        await client.run(
            "CREATE (r:Restriction {id:$id, subject:'микрорайон', object:'озелененная территория', "
            "kind:'минимальная_доля_площади', value_operator:'>=', value_number:25, value_unit:'%', "
            "extraction_text:'Площадь озеленения составляет не менее 25% площади микрорайона.', measurement_json:$measurement}), "
            "(d:Document {doc_id:$id, name:'test'}), (c:Clause {node_id:$id, numbering:'1'}), "
            "(s:Entity {normalized:$subject}), (o:Entity {normalized:$object}), "
            "(k:RestrictionKind {name:$id}), (r)-[:DERIVED_FROM]->(c)-[:IN_DOCUMENT]->(d), "
            "(r)-[:HAS_SUBJECT]->(s), (r)-[:APPLIES_TO]->(o), (r)-[:OF_KIND]->(k)",
            id=rid,
            subject=rid + "-s",
            object=rid + "-o",
            measurement=measurement.model_dump_json(),
        )
        reader = GraphReader(client)
        rows = await reader.get_by_ids([rid])
        assert rows[0]["measurement_json"] == measurement.model_dump_json()
        service = CheckPlanBackfillService(
            reader, GraphWriter(client), CheckPlanPlanner()
        )
        preview = await service.regenerate(
            rid, CheckPlanRegenerateRequest(expected_revision=0)
        )
        saved = await service.regenerate(
            rid, CheckPlanRegenerateRequest(expected_revision=0, dry_run=False)
        )
        assert saved.plan == preview.plan
        assert saved.revision == 1
        assert saved.plan.template == "zonal_ratio"
        assert saved.plan.params["threshold"] == 25
        assert [
            (x.role, x.entity) for x in saved.plan.declared_requirements.layers
        ] == [("zones", "микрорайон"), ("numerator", "озелененная территория")]
        assert saved.plan.source.document_name == "test"
    finally:
        await client.run(
            "MATCH (n) WHERE n.id=$id OR n.doc_id=$id OR n.node_id=$id OR n.name=$id "
            "OR n.normalized IN [$subject,$object] OR n.restriction_id=$id DETACH DELETE n",
            id=rid,
            subject=rid + "-s",
            object=rid + "-o",
        )
        await client.close()
