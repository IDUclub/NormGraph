import json
from pathlib import Path

import pytest

from src.dto.check_plan import validate_check_plan

CASES = json.loads(
    (Path(__file__).parents[1] / "contract" / "check_plan_cases.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["name"])
def test_shared_check_plan_contract(case):
    if case["valid"]:
        validate_check_plan(case["plan"])
    else:
        with pytest.raises(Exception):
            validate_check_plan(case["plan"])
