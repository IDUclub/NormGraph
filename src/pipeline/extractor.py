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

from src.pipeline.clause_context import ClauseContext, RelatedClause
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
_CARDINAL_FORMS = {
    0: "ноль нуль ноля нуля нолю нулю нолем нулем ноле нуле",
    1: "один одна одно одни одного одной одному одним одну одном одних одними одною",
    2: "два две двух двум двумя",
    3: "три трех трем тремя",
    4: "четыре четырех четырем четырьмя",
    5: "пять пяти пятью",
    6: "шесть шести шестью",
    # "семью" is left out: it is also "семья" (family) in the accusative.
    7: "семь семи",
    8: "восемь восьми восемью восьмью",
    9: "девять девяти девятью",
    10: "десять десяти десятью",
    11: "одиннадцать одиннадцати одиннадцатью",
    12: "двенадцать двенадцати двенадцатью",
    13: "тринадцать тринадцати тринадцатью",
    14: "четырнадцать четырнадцати четырнадцатью",
    15: "пятнадцать пятнадцати пятнадцатью",
    16: "шестнадцать шестнадцати шестнадцатью",
    17: "семнадцать семнадцати семнадцатью",
    18: "восемнадцать восемнадцати восемнадцатью",
    19: "девятнадцать девятнадцати девятнадцатью",
    20: "двадцать двадцати двадцатью",
    30: "тридцать тридцати тридцатью",
    40: "сорок сорока",
    50: "пятьдесят пятидесяти пятьюдесятью",
    60: "шестьдесят шестидесяти шестьюдесятью",
    70: "семьдесят семидесяти семьюдесятью",
    80: "восемьдесят восьмидесяти восемьюдесятью восьмьюдесятью",
    90: "девяносто девяноста",
    100: "сто ста",
    200: "двести двухсот двумстам двумястами двухстах",
    300: "триста трехсот тремстам тремястами трехстах",
    400: "четыреста четырехсот четыремстам четырьмястами четырехстах",
    500: "пятьсот пятисот пятистам пятьюстами пятистах",
    600: "шестьсот шестисот шестистам шестьюстами шестистах",
    700: "семьсот семисот семистам семьюстами семистах",
    800: "восемьсот восьмисот восьмистам восемьюстами восьмьюстами восьмистах",
    900: "девятьсот девятисот девятистам девятьюстами девятистах",
    1000: (
        "тысяча тысячи тысяче тысячу тысячей тысячью тысячею "
        "тысяч тысячам тысячами тысячах"
    ),
    1.5: "полтора полторы полутора",
}
_CARDINALS = {
    form: number for number, forms in _CARDINAL_FORMS.items() for form in forms.split()
}
_WORD_NUMBER = re.compile(
    r"\b(?:" + "|".join(_CARDINALS) + r")(?:\s+(?:" + "|".join(_CARDINALS) + r"))*\b"
)


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


def _quote_in(quote: str, tokens: list[str], text: str) -> bool:
    return quote in text or _is_ordered_subsequence(tokens, _grounding_tokens(text))


def _numerals_to_digits(text: str) -> str:
    """Convert cardinal phrases only for quantity checks, preserving quotes and spans."""

    def replace(match: re.Match) -> str:
        numbers = [_CARDINALS[word] for word in match[0].split()]
        if len(numbers) == 1:
            return str(numbers[0])
        total, group, previous = 0, 0, 4
        for number in numbers:
            if number == 1000 and not total:
                total = (group or 1) * 1000
                group, previous = 0, 4
                continue
            rank = 3 if number >= 100 else 2 if number >= 10 else 1
            # Descending parts form a cardinal; adjacent independent numbers do not.
            if number in {0, 1.5, 1000} or rank >= previous:
                return match[0]
            group += number
            previous = 1 if 10 <= number < 20 else rank
        return str(total + group)

    return _WORD_NUMBER.sub(replace, text)


def _states_quantity(text: str, value: RestrictionValue) -> bool:
    """Whether normalised ``text`` states the value's number in its unit."""
    text = _numerals_to_digits(text)
    # Ranges and dimensions share the trailing unit with each preceding number.
    number = r"\d+(?:[.,]\d+)?"
    separator = r"(?: - |\s*[xх×*]\s*|\s+и\s+(?:(?:не более|не менее|до)\s+)?)"
    spread = re.sub(
        rf"({number}){separator}({number})\s*([а-яa-z%]+)",
        r"\1 \3 \2 \3",
        text,
    )
    unit = _normalize_for_grounding(value.unit or "").rstrip(".")
    aliases = _UNIT_ALIASES.get(unit)
    # Normalisation spaces out dashes, so only the magnitude can be checked.
    return any(
        float(n.replace(",", ".")) == abs(value.number)
        and (aliases is None or u in aliases)
        for n, u in _QUANTITY.findall(spread)
    )


# The restriction was read from a linked clause only: that clause yields it itself.
_CONTEXT_ONLY = "context_only"


def _ground(
    restriction: ExtractedRestriction,
    clause: str,
    related: list[tuple[RelatedClause, str]] | tuple = (),
) -> tuple[str | None, dict | None]:
    """``(reason, value_source)``: why a restriction is not supported, else where its value is.

    The quote must be a substring of the clause, or — for list items the model joins with
    their lead-in ("не менее: 4,2 м — при высоте ...") — its words and every number must
    occur in the clause in the same order. ``related`` are the normalised linked clauses
    the model saw: a quote inside one of them belongs to that clause; the lead-in of a quote
    may come from one when its end is the clause's own, and a value missing from the quote
    and the clause may be stated there.
    """
    quote = _normalize_for_grounding(restriction.extraction_text).strip(' ".,;:')
    tokens = _grounding_tokens(quote)
    if not tokens:
        return "ungrounded_extraction_text", None
    if not _quote_in(quote, tokens, clause):
        if any(_quote_in(quote, tokens, text) for _, text in related):
            return _CONTEXT_ONLY, None
        own = _grounding_tokens(clause)
        # The lead-in is linked, the rest — at least two words or numbers — is the clause.
        if not any(
            _is_ordered_subsequence(tokens, _grounding_tokens(text) + own)
            for _, text in related
        ) or sum(token in set(own) for token in tokens) < min(2, len(set(own))):
            return "ungrounded_extraction_text", None
    value = restriction.value
    if not (value and value.number is not None and value.unit):
        return None, None
    quoted = _states_quantity(quote, value)
    if quoted and _states_quantity(clause, value):
        return None, None
    for item, text in related:
        if _states_quantity(text, value):
            return None, item.source()
    # A quoted value is grounded with the quote, even if its lead-in is linked.
    return (None, None) if quoted else ("ungrounded_extraction_quantity", None)


def _shift_span(restriction: ExtractedRestriction, offset: int) -> None:
    """Make the span relative to the clause; a span inside the linked prefix has none."""
    if not offset or restriction.char_start is None:
        return
    start, end = restriction.char_start - offset, restriction.char_end
    end = None if end is None else end - offset
    restriction.char_start = start if start >= 0 else None
    restriction.char_end = end if end is not None and end > 0 and start >= 0 else None


def _ungrounded_reason(restriction: ExtractedRestriction, clause: str) -> str | None:
    """Why a restriction is not supported by its clause text, or ``None`` if it is."""
    return _ground(restriction, clause)[0]


class RestrictionExtractor:
    def __init__(
        self,
        model: ProviderLanguageModel,
        *,
        extraction_passes: int = 1,
        max_char_buffer: int = 1500,
        context_chars: int = 3000,
    ) -> None:
        self._model = model
        self._passes = extraction_passes
        self._max_char_buffer = max_char_buffer
        # Characters of linked clauses shown with a clause; 0 extracts the clause alone.
        self._context_chars = max(0, int(context_chars))

    def extract_clause_sync(
        self, text: str, context: ClauseContext | None = None
    ) -> list[ExtractedRestriction]:
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
        context = context or ClauseContext()
        # Linked clauses are read before the clause, in the same chunk.
        prefix = context.extraction_prefix(self._context_chars)
        annotated = lx.extract(
            text_or_documents=prefix + text,
            prompt_description=PROMPT_DESCRIPTION,
            examples=EXAMPLES,
            model=self._model,
            fence_output=True,
            use_schema_constraints=False,
            resolver_params={"suppress_parse_errors": False},
            extraction_passes=self._passes,
            max_char_buffer=self._max_char_buffer + len(prefix),
            show_progress=False,
        )
        restrictions = to_restrictions(annotated)
        grounded = []
        rejected: list[str] = []
        from_context = 0
        clause = _normalize_for_grounding(text)
        related = [
            (item, _normalize_for_grounding(shown))
            for item, shown in context.prefix_clauses(self._context_chars)
        ]
        unresolved = context.unresolved_labels()
        for restriction in restrictions:
            _shift_span(restriction, len(prefix))
            # An invented quotation/number must never become an executable norm.
            reason, value_source = _ground(restriction, clause, related)
            if reason == _CONTEXT_ONLY:
                from_context += 1  # the linked clause yields it from its own text
                continue
            if reason:
                rejected.append(reason)
                continue
            restriction.value_source = value_source
            value = restriction.value
            if unresolved and (value is None or value.number is None):
                restriction.unresolved_references = unresolved
            grounded.append(restriction)
        if from_context:
            log.info("restrictions_from_linked_clauses_dropped", dropped=from_context)
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

    async def extract_clause(
        self, text: str, context: ClauseContext | None = None
    ) -> list[ExtractedRestriction]:
        return await asyncio.to_thread(self.extract_clause_sync, text, context)
