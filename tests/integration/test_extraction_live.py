"""Integration: real langextract extraction via the configured LLM — self-skips when down.

Exercises the full extraction seam (prompt + examples → provider → LLM → parsed triples) against
whatever ``NG_LLM_*`` points at. Model quality varies, so the assertions are structural, not exact.
"""

from __future__ import annotations

import httpx
import pytest

from src.common.config import settings
from src.pipeline.extractor import RestrictionExtractor
from src.providers import build_llm
from src.providers.langextract_backend import ProviderLanguageModel

SAMPLE = (
    "В границах санитарно-защитной зоны не допускается использование "
    "земельных участков для размещения жилой застройки."
)


@pytest.mark.integration
def test_extract_clause_against_live_llm():
    llm = build_llm(settings)
    model = ProviderLanguageModel(
        llm, model_id=settings.llm_model, temperature=settings.llm_temperature
    )
    extractor = RestrictionExtractor(model)
    try:
        restrictions = extractor.extract_clause_sync(SAMPLE)
    except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
        pytest.skip(f"LLM unavailable or extraction failed: {exc}")

    assert isinstance(restrictions, list)
    for r in restrictions:
        assert r.subject and r.object and r.kind
