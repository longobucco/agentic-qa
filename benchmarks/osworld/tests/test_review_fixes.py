"""Review fixes R1 (legacy Astra prompt never offers run_python) and R4 (refuse campaigns
without the pinned OSWorld evaluator/SetupController code)."""
from unittest.mock import patch

import pytest

from benchmarks.osworld import config, prompts
from benchmarks.osworld.env import osworld_eval


def test_astra_prompt_never_offers_run_python(monkeypatch):
    monkeypatch.setattr(config, "RESTRICT_RUN_PYTHON", False)
    from benchmarks.osworld.runners import gpt_astra
    assert "run_python" not in gpt_astra._astra_prompt({"instruction": "x"})


def test_agent_prompt_explicit_override(monkeypatch):
    monkeypatch.setattr(config, "RESTRICT_RUN_PYTHON", False)
    assert "run_python" in prompts.agent_prompt({"instruction": "x"})
    assert "run_python" not in prompts.agent_prompt({"instruction": "x"}, offer_run_python=False)


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
