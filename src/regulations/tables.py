"""Read an IDU_DVD ``table_html`` into rows of cell texts (stdlib only)."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

_SPACE = re.compile(r"\s+")


def clean(text: str) -> str:
    return _SPACE.sub(" ", unescape(text or "")).strip()


class _TableReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            try:
                self._span = max(1, int(dict(attrs).get("colspan") or 1))
            except ValueError:
                self._span = 1
        elif tag in ("p", "br") and self._cell is not None and self._cell:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            text = "".join(self._cell)
            lines = [clean(line) for line in text.split("\n")]
            self._row.append("\n".join(line for line in lines if line))
            # a spanning cell keeps its text once: the others stay empty
            self._row.extend([""] * (self._span - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def table_rows(html: str | None) -> list[list[str]]:
    """Rows of a table, each a list of cell texts (a ``colspan`` cell is followed by blanks)."""
    if not html:
        return []
    reader = _TableReader()
    reader.feed(html)
    reader.close()
    return [row for row in reader.rows if any(cell for cell in row)]
