"""Restriction extractor: runs langextract over a clause and maps the result to our model.

``langextract`` is synchronous and CPU/IO-bound on the LLM call, so the async entry point runs it
in a worker thread. The mapping from a langextract ``AnnotatedDocument`` to ``ExtractedRestriction``
objects is a pure function (``to_restrictions``) so it can be unit-tested without a live model.
"""

from __future__ import annotations

import asyncio

import langextract as lx
import structlog

from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.prompts import EXAMPLES, PROMPT_DESCRIPTION, RESTRICTION_CLASS
from src.providers.langextract_backend import ProviderLanguageModel

log = structlog.get_logger(__name__)

# Attribute keys consumed explicitly; everything else is preserved under ``extra``.
_KNOWN_ATTRS = {
    "subject",
    "object",
    "kind",
    "value_operator",
    "value_number",
    "value_unit",
    "value_condition",
}


def _attr_str(value) -> str:
    """Attributes are supposed to be strings, but the model may emit a list, a number or a
    nested value — flatten to a stripped string instead of crashing the whole document sync."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if v is not None).strip(", ").strip()
    return str(value).strip()


def _parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _value_from_attrs(attrs: dict) -> RestrictionValue | None:
    value = RestrictionValue(
        operator=_attr_str(attrs.get("value_operator")) or None,
        number=_parse_number(attrs.get("value_number")),
        unit=_attr_str(attrs.get("value_unit")) or None,
        condition=_attr_str(attrs.get("value_condition")) or None,
    )
    return None if value.is_empty() else value


def to_restrictions(annotated: lx.data.AnnotatedDocument) -> list[ExtractedRestriction]:
    """Map a langextract result to restriction triples, dropping malformed extractions."""
    out: list[ExtractedRestriction] = []
    for ext in annotated.extractions or []:
        if ext.extraction_class != RESTRICTION_CLASS:
            continue
        attrs = dict(ext.attributes or {})
        subject = _attr_str(attrs.get("subject"))
        object_ = _attr_str(attrs.get("object"))
        kind = _attr_str(attrs.get("kind"))
        if not (subject and object_ and kind):
            continue
        interval = ext.char_interval
        out.append(
            ExtractedRestriction(
                subject=subject,
                object=object_,
                kind=kind,
                value=_value_from_attrs(attrs),
                extraction_text=ext.extraction_text or "",
                char_start=getattr(interval, "start_pos", None),
                char_end=getattr(interval, "end_pos", None),
                extra={k: v for k, v in attrs.items() if k not in _KNOWN_ATTRS},
            )
        )
    return out


class RestrictionExtractor:
    def __init__(
        self,
        model: ProviderLanguageModel,
        *,
        extraction_passes: int = 1,
        max_char_buffer: int = 1500,
    ) -> None:
        self._model = model
        self._passes = extraction_passes
        self._max_char_buffer = max_char_buffer

    def extract_clause_sync(self, text: str) -> list[ExtractedRestriction]:
        if not text.strip():
            return []
        annotated = lx.extract(
            text_or_documents=text,
            prompt_description=PROMPT_DESCRIPTION,
            examples=EXAMPLES,
            model=self._model,
            fence_output=True,
            use_schema_constraints=False,
            extraction_passes=self._passes,
            max_char_buffer=self._max_char_buffer,
            show_progress=False,
        )
        return to_restrictions(annotated)

    async def extract_clause(self, text: str) -> list[ExtractedRestriction]:
        return await asyncio.to_thread(self.extract_clause_sync, text)
