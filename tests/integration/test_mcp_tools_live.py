"""Integration: MCP tools over a live Neo4j via an in-memory FastMCP client — self-skips when down.

Seeds a tiny restriction graph, then drives the MCP server exactly as a client (gMART) would and
asserts the tools return the expected data. Only tools that need no LLM/embeddings are exercised
(filter-only search, detail, traversal, facets), so the test needs just Neo4j.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastmcp import Client

from src.dependencies import init_dependencies
from src.mcp_server.server import mcp


def _data(result):
    """Plain dict/list payload of a FastMCP call_tool result (uniform across return types)."""
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        # FastMCP wraps non-object results (e.g. a list) as {"result": ...}.
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    if isinstance(sc, list):
        return sc
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_tools_over_live_graph():
    deps = init_dependencies()
    try:
        await deps.graph.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    t = uuid.uuid4().hex[:8]
    kind = f"mcpkind_{t}"
    w = deps.writer
    try:
        await w.upsert_document({"doc_id": f"d-{t}", "name": f"СП {t}"})
        await w.upsert_clause(
            {"node_id": f"c-{t}", "doc_id": f"d-{t}", "text": "x", "numbering": "1.1"}
        )
        await w.ensure_kind(kind)
        await w.upsert_entity(f"subj-{t}", name="s")
        await w.upsert_entity(f"obj-{t}", name="o")
        for rid in (f"r1-{t}", f"r2-{t}"):
            await w.upsert_restriction(
                {"id": rid, "subject": "S", "object": "O", "kind": kind},
                clause_node_id=f"c-{t}",
                subject_normalized=f"subj-{t}",
                object_normalized=f"obj-{t}",
                kind_name=kind,
            )
        await w.link_shares_entity(f"r1-{t}")

        async with Client(mcp) as client:
            # health
            h = _data(await client.call_tool("health", {}))
            assert h["status"] == "ok"

            # facets: our kind shows up with its count
            kinds = _data(await client.call_tool("list_restriction_kinds", {}))
            ours = [k for k in kinds if k["name"] == kind]
            assert ours and ours[0]["restriction_count"] == 2

            # filter-only search (no query → no embeddings needed)
            sr = _data(await client.call_tool("search_restrictions", {"kind": kind}))
            assert sr["count"] == 2
            ids = {hit["id"] for hit in sr["hits"]}
            assert ids == {f"r1-{t}", f"r2-{t}"}

            # detail with neighbours
            detail = _data(
                await client.call_tool("get_restriction", {"restriction_id": f"r1-{t}"})
            )
            assert detail["id"] == f"r1-{t}"
            assert {n["restriction"]["id"] for n in detail["neighbors"]} == {f"r2-{t}"}

            # traversal
            graph = _data(
                await client.call_tool(
                    "traverse_restrictions", {"restriction_id": f"r1-{t}", "depth": 2}
                )
            )
            assert {n["id"] for n in graph["nodes"]} == {f"r1-{t}", f"r2-{t}"}

            # entities facet
            ents = _data(
                await client.call_tool("list_entities", {"query": f"subj-{t}"})
            )
            assert any(e["normalized"] == f"subj-{t}" for e in ents)
    finally:
        await deps.graph.run(
            "MATCH (n) WHERE n.id ENDS WITH $t OR n.doc_id ENDS WITH $t "
            "OR n.node_id ENDS WITH $t OR n.normalized ENDS WITH $t OR n.name = $k "
            "DETACH DELETE n",
            t=t,
            k=kind,
        )
        await deps.aclose()
