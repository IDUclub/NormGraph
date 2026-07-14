"""Tolerant Avro model resolution for the otteroad consumer.

otteroad resolves an incoming ``schema_id`` to an :class:`AvroEventModel` subclass by an EXACT
string comparison of ``json.dumps(model.avro_schema(), separators=(",", ":"))`` against the schema
string stored in the Schema Registry (``AvroSerializerMixin._get_model_class``). The IDU contour
registry holds ``document.events.documents.DocumentProcessed`` (the schema id IDU_DVD tags its
messages with) with the metadata keys ordered ``...,"doc":...,"default":null`` — an ordering an
older otteroad produced — while the otteroad version we run emits ``...,"default":null,"doc":...``.
The two schemas are canonically identical (only the order of the non-structural ``doc``/``default``
keys differs), yet the exact string compare fails and every ``DocumentProcessed`` event is silently
dropped.

The producer side is unaffected: the registry treats the schemas as equal and hands IDU_DVD back
the existing schema id. This shim makes the consumer just as tolerant — after the exact match fails
it retries with an order-insensitive (canonical) comparison, so a semantically identical schema
still resolves to its model. The patch is idempotent and delegates to otteroad's own behaviour if
the registry fetch or schema parse fails.
"""

from __future__ import annotations

import json

import structlog

log = structlog.get_logger(__name__)


def canonical_schema(schema_str: str) -> str:
    """Order-insensitive canonical form of an Avro schema JSON string."""
    return json.dumps(json.loads(schema_str), sort_keys=True, separators=(",", ":"))


def install_tolerant_schema_matching() -> None:
    """Patch otteroad model resolution to accept canonically-equal schemas (idempotent)."""
    from otteroad.avro import AvroEventModel
    from otteroad.avro.serializer import AvroSerializerMixin

    if getattr(AvroSerializerMixin, "_ng_tolerant_schema_matching", False):
        return
    original = AvroSerializerMixin._get_model_class

    def _get_model_class(self, schema_id: int):
        if schema_id in self._schema_cache:
            return self._schema_cache[schema_id]
        try:
            schema_str = self._get_schema_str(schema_id)
            target = canonical_schema(schema_str)
        except Exception:  # pragma: no cover - registry/parse failure -> otteroad's own path
            return original(self, schema_id)
        for model in AvroEventModel.__subclasses__():
            generated = json.dumps(model.avro_schema(), separators=(",", ":"))
            if generated == schema_str or canonical_schema(generated) == target:
                self._schema_cache[schema_id] = model
                return model
        self._logger.warning("No registered model for given schema", schema_id=schema_id)
        return None

    AvroSerializerMixin._get_model_class = _get_model_class
    AvroSerializerMixin._ng_tolerant_schema_matching = True
    log.info("otteroad_tolerant_schema_matching_installed")
