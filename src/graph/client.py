"""Async Neo4j client.

Thin wrapper around the official async driver that owns a single shared driver instance for the
process. Graph schema (constraints + native vector indexes) is provisioned by ``src.graph.schema``
at startup; query helpers live in ``src.graph.queries``. This module only owns the connection.
"""

from __future__ import annotations

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase

log = structlog.get_logger(__name__)


class Neo4jClient:
    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str = "neo4j",
    ) -> None:
        self._uri = uri
        self._database = database
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            uri, auth=(user, password)
        )

    @property
    def database(self) -> str:
        return self._database

    @property
    def driver(self) -> AsyncDriver:
        return self._driver

    async def verify_connectivity(self) -> None:
        """Raise if the database is unreachable — used for readiness/health checks."""
        await self._driver.verify_connectivity()

    async def run(self, query: str, **params) -> list[dict]:
        """Execute a Cypher query and return all records as plain dicts."""
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, **params)
            return [record.data() async for record in result]

    async def close(self) -> None:
        await self._driver.close()
