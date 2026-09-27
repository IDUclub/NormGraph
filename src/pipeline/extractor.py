"""Restriction extractor: runs langextract over a clause and maps the result to our model.

``langextract`` is synchronous and CPU/IO-bound on the LLM call, so the async entry point runs it
in a worker thread. The mapping from a langextract ``AnnotatedDocument`` to ``ExtractedRestriction``
objects is a pure function (``to_restrictions``) so it can be unit-tested without a live model.
"""

from __future__ import annotations

import asyncio
import re

import langextract as lx
import structlog

from src.pipeline.models import (
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)
from src.pipeline.prompts import EXAMPLES, PROMPT_DESCRIPTION, RESTRICTION_CLASS
from src.pipeline.spatial_rules import compile_spatial_rule
from src.providers.langextract_backend import (
    InvalidExtractionOutput,
    ProviderLanguageModel,
)

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
    "measurement_kind",
    "measurement_indicator",
    "measurement_basis",
    "measurement_numerator_entity",
    "measurement_denominator_entity",
}


def _attr_str(value) -> str:
    """Attributes are supposed to be strings, but the model may emit a list, a number or a
    nested value — flatten to a stripped string instead of crashing the whole document sync.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return (
            ", ".join(str(v).strip() for v in value if v is not None)
            .strip(", ")
            .strip()
        )
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
                measurement=_measurement_from_attrs(attrs),
                extraction_text=ext.extraction_text or "",
                char_start=getattr(interval, "start_pos", None),
                char_end=getattr(interval, "end_pos", None),
                extra={k: v for k, v in attrs.items() if k not in _KNOWN_ATTRS},
            )
        )
    return out


def _measurement_from_attrs(attrs: dict) -> RestrictionMeasurement | None:
    values = {
        key: _attr_str(attrs.get("measurement_" + key)) or None
        for key in (
            "kind",
            "indicator",
            "basis",
            "numerator_entity",
            "denominator_entity",
        )
    }
    if not any(values.values()):
        return None
    if values["kind"] not in {
        "area_share",
        "count_share",
        "provision",
        "distance",
        "linear_size",
        "other",
    }:
        values["kind"] = "other"
    return RestrictionMeasurement(**values)


# Typographic variants the model silently normalises when quoting (non-breaking hyphen,
# en/em dashes, guillemets, narrow no-break spaces, superscript units).
_GROUNDING_CHARS = str.maketrans(
    {
        **{c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"},
        **{c: '"' for c in "«»„“”‟"},
        **{c: " " for c in "\u00a0\u2007\u2009\u202f"},
        "²": "2",
        "³": "3",
    }
)
_GROUNDING_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[a-zа-я]+")
_QUANTITY = re.compile(r"(\d+(?:[.,]\d+)?)\s*([а-яa-z%]+)")
_UNIT_ALIASES = {
    "эт": {"этаж", "этажа", "этажей"},
    "этажей": {"этаж", "этажа", "этажей"},
    "м": {"м", "метр", "метра", "метров"},
    "км": {"км", "километр", "километра", "километров"},
    "%": {"%", "процент", "процента", "процентов"},
}


def _normalize_for_grounding(value: str) -> str:
    text = " ".join(
        value.casefold().replace("ё", "е").translate(_GROUNDING_CHARS).split()
    )
    text = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", text)  # "1 000" -> "1000"
    # One spacing for every dash, so "— 5 м" never reads as "-5".
    return re.sub(r" ?- ?", " - ", text)


def _grounding_tokens(text: str) -> list[str]:
    # Numbers must match exactly; words by a short stem to tolerate Russian inflection
    # ("запрещаются" quoted as "запрещается").
    return [
        tok.replace(",", ".") if tok[0].isdigit() else tok[:5]
        for tok in _GROUNDING_TOKEN.findall(text)
    ]


def _is_ordered_subsequence(needle: list[str], haystack: list[str]) -> bool:
    remaining = iter(haystack)
    return all(tok in remaining for tok in needle)


def _ungrounded_reason(restriction: ExtractedRestriction, clause: str) -> str | None:
    """Why a restriction is not supported by its clause text, or ``None`` if it is.

    The quote must be a substring of the clause, or — for list items the model joins with
    their lead-in ("не менее: 4,2 м — при высоте ...") — its words and every number must
    occur in the clause in the same order.
    """
    quote = _normalize_for_grounding(restriction.extraction_text).strip(' ".,;:')
    tokens = _grounding_tokens(quote)
    if not tokens or (
        quote not in clause
        and not _is_ordered_subsequence(tokens, _grounding_tokens(clause))
    ):
        return "ungrounded_extraction_text"
    value = restriction.value
    if value and value.number is not None and value.unit:
        # A range shares its unit: "10 - 40 м" states both 10 м and 40 м.
        spread = re.sub(
            r"(\d+(?:[.,]\d+)?) - (\d+(?:[.,]\d+)?) ?([а-яa-z%]+)",
            r"\1 \3 \2 \3",
            quote,
        )
        unit = _normalize_for_grounding(value.unit).rstrip(".")
        aliases = _UNIT_ALIASES.get(unit)
        # Normalisation spaces out dashes, so only the magnitude can be checked.
        if not any(
            float(n.replace(",", ".")) == abs(value.number)
            and (aliases is None or u in aliases)
            for n, u in _QUANTITY.findall(spread)
        ):
            return "ungrounded_extraction_quantity"
    return None


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
        if re.fullmatch(
            r"\s*\d+(?:\.\d+)*\s+(?:Этажность|Наличие|Расстояния|Доля|Набор)[А-Яа-яЁё\s-]*",
            text,
        ) and not re.search(
            r"долж|следует|не менее|не более|превыш|огранич|требуе|запрещ|допуска",
            text,
            re.I,
        ):
            return []
        # Preserve coupled quantities (e.g. all distance bands) and object roles
        # before a language model can split or reverse them.
        if rule := compile_spatial_rule(text):
            return [rule.restriction]
        annotated = lx.extract(
            text_or_documents=text,
            prompt_description=PROMPT_DESCRIPTION,
            examples=EXAMPLES,
            model=self._model,
            fence_output=True,
            use_schema_constraints=False,
            resolver_params={"suppress_parse_errors": False},
            extraction_passes=self._passes,
            max_char_buffer=self._max_char_buffer,
            show_progress=False,
        )
        restrictions = to_restrictions(annotated)
        grounded = []
        rejected: list[str] = []
        clause = _normalize_for_grounding(text)
        for restriction in restrictions:
            # An invented quotation/number must never become an executable norm.
            reason = _ungrounded_reason(restriction, clause)
            if reason:
                rejected.append(reason)
                continue
            grounded.append(restriction)
        if rejected:
            # Never log the quote or clause: documents can be private.
            log.warning(
                "restrictions_ungrounded",
                dropped=len(rejected),
                kept=len(grounded),
                reasons=sorted(set(rejected)),
            )
            if not grounded:
                # Nothing usable came back: fail the clause so a replacement keeps the
                # previous extraction and the clause is listed for reprocessing.
                raise InvalidExtractionOutput(rejected[0])
        return grounded

    async def extract_clause(self, text: str) -> list[ExtractedRestriction]:
        return await asyncio.to_thread(self.extract_clause_sync, text)
