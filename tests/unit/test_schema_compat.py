"""Tolerant otteroad schema matching — canonically-equal schemas must resolve to their model.

Reproduces the contour mismatch: the registry stores DocumentProcessed with the doc/default
metadata keys in a different order than the running otteroad generates. The exact string compare
otteroad uses fails; the canonical fallback installed by schema_compat must still resolve the model.
"""

from __future__ import annotations

import copy
import json

from src.sync.events import DocumentProcessed
from src.sync.schema_compat import (
    canonical_schema,
    install_tolerant_schema_matching,
)


def _reordered_registry_schema() -> str:
    """DocumentProcessed's schema with doc placed before default in nullable fields
    (the ordering the contour registry stores, which the current otteroad does not emit)."""
    # Deep-copy: avro_schema() may return a shared/cached dict — mutating it in place would
    # corrupt the schema for every other test in the session (e.g. the frozen-schema guard).
    schema = copy.deepcopy(DocumentProcessed.avro_schema())
    for field in schema["fields"]:
        if "default" in field and "doc" in field:
            reordered = {}
            for key in ("name", "type", "doc", "default"):
                if key in field:
                    reordered[key] = field[key]
            field.clear()
            field.update(reordered)
    return json.dumps(schema, separators=(",", ":"))


class _FakeSerializer:
    """Stand-in exposing the internals otteroad's _get_model_class reaches for."""

    def __init__(self, schema_str: str) -> None:
        self._schema_cache: dict[int, object] = {}
        self._schema_str = schema_str

        class _Logger:
            def warning(self, *a, **k):
                pass

        self._logger = _Logger()

    def _get_schema_str(self, schema_id: int) -> str:
        return self._schema_str


def test_reordered_schema_differs_by_string_but_is_canonically_equal():
    exact = json.dumps(DocumentProcessed.avro_schema(), separators=(",", ":"))
    reordered = _reordered_registry_schema()
    assert reordered != exact  # the exact compare otteroad uses would fail
    assert canonical_schema(reordered) == canonical_schema(exact)


def test_tolerant_matching_resolves_reordered_schema():
    from otteroad.avro.serializer import AvroSerializerMixin

    install_tolerant_schema_matching()
    fake = _FakeSerializer(_reordered_registry_schema())
    model = AvroSerializerMixin._get_model_class(fake, 13)
    assert model is DocumentProcessed
    assert fake._schema_cache[13] is DocumentProcessed


def test_install_is_idempotent():
    from otteroad.avro.serializer import AvroSerializerMixin

    install_tolerant_schema_matching()
    first = AvroSerializerMixin._get_model_class
    install_tolerant_schema_matching()
    assert AvroSerializerMixin._get_model_class is first
