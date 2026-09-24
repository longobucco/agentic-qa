"""Review fix R4: refuse campaigns without the pinned OSWorld evaluator/SetupController code."""
from unittest.mock import patch

import pytest

from benchmarks.osworld.env import osworld_eval


def test_pinned_code_problems_empty_when_everything_is_pinned():
    assert osworld_eval.pinned_code_problems() == []


def test_pinned_code_problems_reports_a_missing_setup_controller_pin():
    fake = {"evaluator_commit": osworld_eval.UPSTREAM_COMMIT, "setup_controller_commit": None}
    with patch.object(osworld_eval, "evaluator_provenance", return_value=fake):
        problems = osworld_eval.pinned_code_problems()
    assert problems and "setup_controller" in problems[0]
    with patch.object(osworld_eval, "evaluator_provenance", return_value=fake):
        with pytest.raises(SystemExit):
            osworld_eval.pinned_code_preflight()
