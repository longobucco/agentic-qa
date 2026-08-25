"""G2 (contamination half): does the model already "know" the ground truth of a task from
training data, without touching an environment? One no-tools `claude -p` call per sampled
task, no sandbox, no MCP.

Not proof of contamination -- a confident, correct-looking answer could be genuine
capability on an easy task. What it can show: the model naming the dataset unprompted, or a
claimed value matching our ground truth exactly on a task where a blind guess is
implausible. A light indicator (RQ6), not a forensic audit.

Run: python -m benchmarks.osworld.analysis.g2_contamination
"""
import json

from benchmarks.osworld import config
from core.agent_loop import build_claude_cmd, run_claude

PROBE_APPS = ["chrome", "libreoffice_calc", "libreoffice_writer", "libreoffice_impress",
              "gimp", "thunderbird", "vlc", "vscode", "os"]

PROBE_PROMPT = """Below is a task instruction, given with no context about where it comes from.
Do NOT attempt to solve it or produce a plausible-sounding answer. Answer only these two questions, honestly:

1. Do you recognize this exact instruction as coming from a specific public benchmark dataset (e.g. OSWorld)? If yes, name the dataset and the task id if you know it.
2. Do you know the precise expected/reference value this instruction is checked against -- not a guess, only if you are confident you have seen the actual ground truth? If yes, state it exactly. If not, say "UNKNOWN" -- do not fabricate a plausible value.

Instruction:
{instruction}
"""


def sample(tasks_path=None):
    tasks = []
    with open(tasks_path or config.TASKS_FILE) as f:
        for line in f:
            tasks.append(json.loads(line))
    out = []
    for app in PROBE_APPS:
        cands = sorted(
            (t for t in tasks if (t.get("related_apps") or [None])[0] == app
             and t["evaluator"].get("func") != "infeasible"),
            key=lambda t: t["id"])
        if cands:
            out.append(cands[0])
    return out


def _expected_value(task):
    ev = task.get("evaluator", {})
    exp = ev.get("expected")
    if isinstance(exp, dict) and exp.get("type") == "rule":
        return exp.get("rules")
    return exp


def probe(task, *, timeout=60):
    prompt = PROBE_PROMPT.format(instruction=task["instruction"])
    cmd = build_claude_cmd(prompt, max_turns=1)
    response = run_claude(cmd, timeout=timeout)
    expected = _expected_value(task)
    named_dataset = "osworld" in response.lower()
    # search the whole response, not a fixed prefix -- Q2's answer comes after Q1's
    # explanation, which varies in length
    claimed_value = "unknown" not in response.lower()
    return {
        "id": task["id"], "app": (task.get("related_apps") or ["?"])[0],
        "response": response, "expected_ground_truth": expected,
        "named_osworld_unprompted": named_dataset,
        "claimed_to_know_value": claimed_value,
    }


def run():
    return [probe(t) for t in sample()]


def _main():
    results = run()
    print(f"{len(results)} tasks probed\n")
    for r in results:
        print(f"[{r['app']}] {r['id']}")
        print(f"  named OSWorld unprompted: {r['named_osworld_unprompted']}   "
              f"claimed to know exact value: {r['claimed_to_know_value']}")
        print(f"  ground truth (ours):  {json.dumps(r['expected_ground_truth'])[:120]}")
        print(f"  FULL model response:\n{r['response'].strip()}")
        print()
    n = len(results)
    n_named = sum(r["named_osworld_unprompted"] for r in results)
    n_claimed = sum(r["claimed_to_know_value"] for r in results)
    print(f"summary: named OSWorld unprompted {n_named}/{n}; "
          f"claimed to know the exact ground truth {n_claimed}/{n}")


if __name__ == "__main__":
    _main()
