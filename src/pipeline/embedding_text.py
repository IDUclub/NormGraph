"""Text a restriction is embedded from — shared by extraction and startup re-embedding.

Version 1 embedded only ``subject | object | kind | value``. Questions about *placement* of a
facility then matched any triple with that object (premises, equipment), because the kind label
is a short code. Version 2 appends the clause sentence (``extraction_text``), so the vector also
carries what the norm actually says. Bump the version whenever the text changes: stored vectors
with an older version are recomputed on startup.
"""

from __future__ import annotations

RESTRICTION_EMBEDDING_VERSION = 2


def restriction_embedding_text(
    subject: str,
    object_: str,
    kind: str,
    *,
    value_operator: str | None = None,
    value_number: float | None = None,
    value_unit: str | None = None,
    extraction_text: str | None = None,
) -> str:
    text = f"{subject} | {object_} | {kind}"
    if value_number is not None or value_unit:
        number = "" if value_number is None else f"{value_number:g}"
        text += f" | {value_operator or ''}{number}{value_unit or ''}"
    if extraction_text and extraction_text.strip():
        text += "\n" + " ".join(extraction_text.split())
    return text
