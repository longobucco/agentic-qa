"""G5 pre-registered scaffold-ablation sample -> RQ7 (gap-research-plan.md, G5).

Designed 2026-08-09, not yet launched (real spend, gated per plan §5: strata and budget frozen
before the run).

Two one-factor-at-a-time arms, each diffed against the tasks' existing G3 baseline
(MAX_TURNS=150, OBSERVATION=screenshot+a11y) -- only the ablated condition needs a new run.
libreoffice_calc's aa3a8974-... appears in both arms deliberately: each arm diffs
independently against the shared baseline, so the overlap isn't a confound, and it reuses a
task that already broke the turn budget for the observation question too.

ARM_MAX_TURNS (OSW_MAX_TURNS=300, double the default): the tasks that hit the 150-turn cap at
least once in G3, data-driven from results/agent_computer/ as of 2026-08-09. Infeasible-class
cap-hitters (2bd59342, 6d72aad6) are excluded -- an agent that never recognizes an impossible
task won't converge regardless of budget, so those tasks test infeasibility-recognition
(RQ7's other question), not whether 150 turns is enough for genuine complexity.
  - 0326d92d-...  (libreoffice_calc, strong)  capped 1/3 baseline runs
  - aa3a8974-...  (libreoffice_calc, medium)  capped 2/3 baseline runs
  - 5203d847-...  (thunderbird, medium)       capped 3/3 baseline runs
  - 2ad9387a-...  (chrome, strong)            control, never capped (~30 turns/run in G3)

ARM_OBSERVATION (OSW_OBSERVATION=screenshot, a11y_tree removed): one medium-strength task per
app, contrasting coordinate/grid-heavy apps (gimp, libreoffice_calc) against label/structure-
heavy ones (chrome, vscode).
  - 12086550-...  (chrome)
  - 0ed39f63-...  (vscode) -- swapped in 2026-08-09 for the original pick, 0512bb38-...: that
    task turned out to permanently EVAL_ERROR (unshelled-pipe vm_command_line bug, see
    g3_sample.py) and was excluded from sample() entirely, taking its G3 baseline down with it.
    0ed39f63-... is the next medium-strength vscode candidate with a clean 3x SUCCESS baseline.
  - 06ca5602-...  (gimp)
  - aa3a8974-...  (libreoffice_calc, shared with ARM_MAX_TURNS)

N=3/task/arm (matches G3's N). 8 distinct tasks, 24 new runs. Estimated ~$50-65 from G3's
$1.72/run average (76 scored runs, $130.80) -- roughly 1/5 of G3's spend so far.

Operational blocker resolved 2026-08-09: the 7 distinct tasks' G3 baseline runs were copied
(not moved) to run_N_g3baseline_legacy/ alongside the live run_N/ -- launching G5 with --force
on just these task ids now overwrites run_N/ with the ablated condition while the baseline
stays readable from the _legacy copy for the diff. Ready to launch (see _main()); not yet run
(real spend, gated).
"""

ARM_MAX_TURNS = {
    "env": {"OSW_MAX_TURNS": "300"},
    "ids": [
        "0326d92d-d218-48a8-9ca1-981cd6d064c7",
        "aa3a8974-2e85-438b-b29e-a64df44deb4b",
        "5203d847-2572-4150-912a-03f062254390",
        "2ad9387a-65d8-4e33-ad5b-7580065a27ca",   # control
    ],
}

ARM_OBSERVATION = {
    "env": {"OSW_OBSERVATION": "screenshot"},
    "ids": [
        "12086550-11c0-466b-b367-1d9e75b3910e",
        "0ed39f63-6049-43d4-ba4d-5fa2fe04a951",
        "06ca5602-62ca-47f6-ad4f-da151cde54cc",
        "aa3a8974-2e85-438b-b29e-a64df44deb4b",
    ],
}

N_RUNS = 3


def _main():
    for name, arm in (("ARM_MAX_TURNS", ARM_MAX_TURNS), ("ARM_OBSERVATION", ARM_OBSERVATION)):
        env_str = " ".join(f"{k}={v}" for k, v in arm["env"].items())
        ids_str = " ".join(arm["ids"])
        print(f"{name}  ({len(arm['ids'])} tasks x {N_RUNS} runs = {len(arm['ids']) * N_RUNS}):")
        print(f"  {env_str} python -m benchmarks.osworld.run --ids {ids_str} "
              f"--runs {N_RUNS} --force\n")


if __name__ == "__main__":
    _main()
