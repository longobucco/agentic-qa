import json

from benchmarks.osworld.runners import null_agent


class _Ctrl:
    base_url = "http://controller"


class _Env:
    browser = _Ctrl()
    setup_error = None


TASK = {"id": "t1", "instruction": "do x", "snapshot": "os", "related_apps": ["os"],
        "config": [], "evaluator": {"func": "check"}}


def test_null_agent_scores_the_untouched_state_with_done(monkeypatch, tmp_path):
    waits, scored = [], []
    monkeypatch.setattr(null_agent, "protocol_wait", waits.append)
    monkeypatch.setattr(null_agent, "_score", lambda ctrl, task, answer, out: scored.append(answer)
                        or {"verdict": "FAILURE", "reward": 0.0, "source": "official"})
    monkeypatch.setattr(null_agent, "_capture_eval_state", lambda *a: None)
    monkeypatch.setattr(null_agent, "_a11y_health", lambda ctrl: {})
    assert null_agent.run(TASK, env=_Env(), out=tmp_path) == "DONE"
    assert scored == ["DONE"] and len(waits) == 2
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["provenance"]["agent_runtime"] == "null_agent"
    assert result["agent_steps_used"] == 0
    assert json.loads((tmp_path / "eval.json").read_text())["verdict"] == "FAILURE"


def test_null_agent_setup_error_is_an_environment_error(tmp_path):
    env = _Env(); env.setup_error = "boom"
    assert null_agent.run(TASK, env=env, out=tmp_path) == ""
    assert json.loads((tmp_path / "eval.json").read_text())["verdict"] == "ENVIRONMENT_ERROR"
