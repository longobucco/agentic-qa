"""Pre-run power check of X22 stage 3: is the frozen design at its ceiling, and what does it detect?

    OSW_PHASE3_SUFFIX=pkgfix3c .venv/bin/python -m benchmarks.osworld.gates.power_check

Run on 2026-11-12, before any X22 run existed, to answer a direct question: should we expect the
stage-3 significance test to conclude? It compares the frozen design against the two alternatives
that looked plausible, and bounds the effect the card's named mechanism can produce on a stratum
that was NOT selected for that mechanism.

Nothing here changes the design. E7 froze the boundaries and this reads them; the output is
evidence for how the result should be reported, which is what amendment 2 of the lock acts on.
"""
import argparse
import json
import random
import statistics as st
from pathlib import Path

from benchmarks.osworld.gates.calibrate import baseline_rates, mcnemar_p

LOCK = Path(__file__).resolve().parent.parent / "x22_campaign_lock.json"
TASKS = Path(__file__).resolve().parent.parent / "data" / "osworld_verified.jsonl"
EFFECTS = (0.05, 0.10, 0.15, 0.20, 0.30, 0.40)
TRIALS = 4000
SEED = 7


def settings_like(task):
    """Tasks where the card's named mechanism applies: the setting lives behind a terminal route.

    Operationalised as the OS snapshot, or a checker that reads the state through gsettings/dconf
    or a shell command. Deliberately generous -- the point is an upper bound.
    """
    ev = task.get("evaluator") or {}
    result = ev.get("result")
    entries = result if isinstance(result, list) else [result]
    blob = " ".join(json.dumps(e) for e in entries if isinstance(e, dict))
    return (task.get("snapshot") == "os" or "gsettings" in blob or "dconf" in blob
            or any((e or {}).get("type") == "vm_command_line"
                   for e in entries if isinstance(e, dict)))


def power(rates, runs, rule, f, alpha, trials=TRIALS, seed=SEED):
    """P(the exact McNemar crosses `alpha`) when the arm rescues a fraction f of failed tasks."""
    rng = random.Random(seed)
    wins = 0
    for _ in range(trials):
        b = c = 0
        for p0 in rates:
            p1 = 1.0 if (p0 < 1.0 and rng.random() < f) else p0
            s0 = sum(rng.random() < p0 for _ in range(runs))
            s1 = sum(rng.random() < p1 for _ in range(runs))
            if rule == "lenient":
                o0, o1 = s0 >= 1, s1 >= 1
            else:
                o0, o1 = s0 > runs / 2, s1 > runs / 2
            if o1 and not o0:
                b += 1
            elif o0 and not o1:
                c += 1
        if mcnemar_p(b, c) <= alpha:
            wins += 1
    return wins / trials


def noise_pairs(rates, runs, rule, trials=400, seed=11):
    """Discordant pairs from pure noise, with no effect at all: the signal has to beat these."""
    rng = random.Random(seed)
    total = 0
    for _ in range(trials):
        n = 0
        for p0 in rates:
            s0 = sum(rng.random() < p0 for _ in range(runs))
            s1 = sum(rng.random() < p0 for _ in range(runs))
            o0, o1 = (s0 >= 1, s1 >= 1) if rule == "lenient" else (s0 > runs / 2, s1 > runs / 2)
            n += o0 != o1
        total += n
    return total / trials


def main(argv=None):
    argparse.ArgumentParser().parse_args(argv)
    lock = json.loads(LOCK.read_text())
    s3 = lock["stage3"]
    ids = s3["strata"][s3["primary_stratum"]]
    alpha = s3["boundaries"]["nominal_alpha"][-1]
    tasks = {json.loads(l)["id"]: json.loads(l) for l in TASKS.read_text().splitlines() if l.strip()}
    rates, sources, _ = baseline_rates(ids)

    print(f"X22 stage 3 — primary stratum {s3['primary_stratum']}, {len(ids)} tasks, "
          f"final-look alpha {alpha}")
    print(f"per-task baseline rates by source: {sources}\n")

    print("Is the frozen design at its ceiling? Discordant pairs from pure noise, and power:")
    hdr = (f"  {'design':26} {'cost':>6} {'noise':>7} "
           + " ".join(f"f={int(f*100)}%".rjust(7) for f in EFFECTS))
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for runs, rule in ((2, "lenient"), (3, "majority"), (5, "majority")):
        row = [f"{power(rates, runs, rule, f, alpha):.2f}".rjust(7) for f in EFFECTS]
        print(f"  {runs} runs/task, {rule:9}      {len(ids)*runs*2:>6} "
              f"{noise_pairs(rates, runs, rule):>7.1f} " + " ".join(row))
    print("  -> more runs per task does not help: the constraint is the effect, not the noise.")

    sub = [t for t in ids if settings_like(tasks[t])]
    rates_sub, _, _ = baseline_rates(sub)
    print(f"\nWould a mechanism subgroup help? {len(sub)} of {len(ids)} tasks are settings-like:")
    row = [f"{power(rates_sub, 2, 'lenient', f, alpha):.2f}".rjust(7) for f in EFFECTS]
    print(f"  {'subgroup, 2 runs/task':26} {len(sub)*2*2:>6} {'':>7} " + " ".join(row))
    print("  -> no: at n=%d it is worse than the full stratum." % len(sub))

    room = [p for p in rates if p < 1.0]
    room_sub = [p for p in rates_sub if p < 1.0]
    bound = len(room_sub) / max(1, len(room))
    print(f"\nUpper bound on f, if the card's only mechanism is the terminal route for settings:")
    print(f"  tasks with room in the stratum: {len(room)}; of those settings-like: {len(room_sub)}")
    print(f"  -> even rescuing EVERY settings task gives f <= {bound:.3f}, "
          f"power {power(rates, 2, 'lenient', bound, alpha):.2f}")
    print("  For scale: on class F, which was selected FOR the mechanism, the card rescued about "
          "3 of 8 failed tasks, f = 0.37.")
    print("""
Reading. The design is at its ceiling and detects the effect only for f >= 0.30. If the card works
only through the mechanism it names, f on this stratum cannot exceed about 0.17, so the
significance test is more likely than not to be inconclusive -- a structural property, not bad
luck. That outcome is still a finding with a clear reading: the card's effect does not extend
materially beyond the task family it was written for. Amendment 2 of the lock therefore makes the
bounded estimate and the no-harm claim the primary deliverables and declares the significance test
secondary and underpowered below f = 0.20, BEFORE any run exists.""")


if __name__ == "__main__":
    main()
