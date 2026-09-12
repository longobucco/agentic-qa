#!/usr/bin/env python3
"""Fake `claude` CLI for verify-replan integration tests (docs/verify-replan-minimal-
integration-plan.md Section 12: "Un eseguibile fittizio restituisce envelope diversi in
sequenza"). A real subprocess, invoked exactly the way runners/verify_replan.py invokes the
real CLI -- not a Python-level mock of run_claude_meta -- so these tests exercise the actual
argv-building/subprocess/JSON-parsing plumbing, not just the harness logic above it.

Ignores every CLI flag it's given (-p, --model, --mcp-config, ...); a test cares about the
SEQUENCE of envelopes returned across calls, not about faithfully re-implementing the real
CLI's argument handling.

Env contract (set by the test before prepending this script's directory to PATH as `claude`):
  FAKE_CLAUDE_SEQUENCE   path to a JSON file: {"responses": [envelope, envelope, ...]}
  FAKE_CLAUDE_COUNTER    path to a plain-text call-counter file (created if absent)

Each envelope may carry "__sleep__": N to sleep N seconds before printing (timeout scenario).
Calls past the end of the sequence repeat the last envelope, so a test doesn't need to predict
the exact call count to avoid an IndexError -- it should still assert the exact count itself
where that count matters.
"""
import json
import os
import sys
import time


def main():
    seq_path = os.environ["FAKE_CLAUDE_SEQUENCE"]
    counter_path = os.environ["FAKE_CLAUDE_COUNTER"]
    responses = json.loads(open(seq_path).read())["responses"]

    try:
        index = int(open(counter_path).read().strip())
    except (FileNotFoundError, ValueError):
        index = 0
    open(counter_path, "w").write(str(index + 1))

    envelope = responses[min(index, len(responses) - 1)]
    if "__sleep__" in envelope:
        time.sleep(envelope["__sleep__"])
    print(json.dumps({k: v for k, v in envelope.items() if k != "__sleep__"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
