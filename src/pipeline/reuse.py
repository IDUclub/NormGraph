"""When a clause needs the LLM again.

A clause's restrictions stay valid while its text and the extractor stay the same. The clause
records the hash it was extracted under; a sync of a changed document re-extracts only the
clauses whose hash no longer matches — new or edited text — instead of the whole document.
"""

from __future__ import annotations

import hashlib

#: Bump when extraction changes what it returns for the same text (prompt, schema, model
#: contract): every clause is then extracted again on its document's next sync.
EXTRACTION_VERSION = 1


def extraction_hash(text: str) -> str:
    """The hash a clause with ``text`` is extracted under (whitespace-insensitive)."""
    body = " ".join((text or "").split())
    return hashlib.sha256(f"{EXTRACTION_VERSION}\n{body}".encode("utf-8")).hexdigest()[
        :16
    ]
