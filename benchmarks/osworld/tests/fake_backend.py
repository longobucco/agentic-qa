"""The fake campaign backend of test_campaign.py, shared by its in-process tests and by the
driver subprocesses some of them start (which import it as benchmarks.osworld.tests.fake_backend).
It never touches a sandbox, container or VM."""
import json
from pathlib import Path


class FakeBackend:
    NAME = "fake"
    LOG_SUFFIX = "_fake"

    def __init__(self, sweep_file=None):
        """`sweep_file`: where sweep() records the run id it was asked to sweep, for a test that
        can only look at the driver subprocess from outside."""
        self.swept = []
        self.sweep_file = sweep_file

    def protocol_env(self):
        return {"OSW_BACKEND": "daytona"}

    def harness_knob_defaults(self):
        return {"OSW_FAKE_KNOB": "0"}

    def env_conflicts(self, environ):
        return ["OSW_FAKE_BAD='1' (fake refusal)"] if environ.get("OSW_FAKE_BAD") else []

    def sweep(self, run_id, log):
        if not run_id:
            raise ValueError("empty driver run id")
        self.swept.append(run_id)
        if self.sweep_file is None:
            return 0
        Path(self.sweep_file).write_text(json.dumps({"run_id": run_id}))
        log(f"backend sweep: removed 1 sandbox(es) for driver_run={run_id}")
        return 1
