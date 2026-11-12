"""What each candidate confirmatory stratum actually buys, before any of them is chosen.

    OSW_PHASE3_SUFFIX=pkgfix3c \\
      .venv/bin/python benchmarks/osworld/campaign_analysis/split_calibration.py

"219 -> 181, modest power loss" was asserted, not computed. This computes it, for every candidate
the exposure manifest leaves on the table, using the same machinery the frozen design was
calibrated with (`gates.calibrate.baseline_rates`, `gates.power_check.power`) so the numbers are
comparable to the ones already on record.

Per candidate it reports:

  tasks, and how many still have room    a saturated task cannot show an effect either way
  discordant pairs from pure noise       what any signal has to beat
  power at the declared effect sizes     the significance test's chance of concluding
  no-harm resolution                     the Wilson upper bound on the share of already-passing
                                         tasks the arm breaks, when it breaks none observed --
                                         this is the deliverable that does NOT need a big effect,
                                         so its precision is what a smaller stratum really costs
  runs and days                          2 runs/task/arm, two arms, at the measured throughput

Nothing here chooses. The point is that the size of a confirmatory set should come out of this
trade-off rather than out of a round number.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from math import sqrt
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from benchmarks.osworld.gates.calibrate import baseline_rates          # noqa: E402
from benchmarks.osworld.gates.power_check import noise_pairs, power    # noqa: E402

LOCK = HERE.parent / "x22_campaign_lock.json"
MANIFEST = HERE / "tables" / "exposure_manifest.csv"
EFFECTS = (0.10, 0.20, 0.30, 0.40)
RUNS_PER_DAY = 144          # measured throughput assumption; see the concurrency curve


def wilson_upper(k, n, z=1.96):
    """Upper bound of the Wilson interval -- the honest way to report `0 observed out of n`."""
    if n == 0:
        return 1.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre + half) / d


def candidates():
    lock = json.loads(LOCK.read_text())
    strata = lock["stage3"]["strata"]
    rows = list(csv.DictReader(MANIFEST.open()))
    T = lambda sel: [r["task"] for r in rows if sel(r)]          # noqa: E731
    out = {
        "FROZEN out_of_design (219)": strata["out_of_design"],
        "R3 not in a set, never read run by run": T(
            lambda r: r["in_set"] == "-" and r["adjudicated"] == "False"
            and r["_funnel"] == "False"),
        "R2 never a design input": T(lambda r: r["design_input"] == "False"),
        "primary_out_of_sample (sensitivity)": strata["primary_out_of_sample"],
        "never_cited (sensitivity)": strata["never_cited"],
    }
    # A declared DEV/CONFIRM split of R3, stratified by application: the first half of each app's
    # tasks develops, the second half confirms. Deterministic, so it can be frozen by reference.
    by_app = {}
    for r in rows:
        if r["in_set"] == "-" and r["adjudicated"] == "False" and r["_funnel"] == "False":
            by_app.setdefault(r["app"], []).append(r["task"])
    confirm = []
    for app in sorted(by_app):
        ts = sorted(by_app[app])
        confirm += ts[len(ts) // 2:]
    out["R3 split in half by app -> CONFIRM"] = confirm

    # The realistic shape: a DEV tranche aimed at the blind applications, and CONFIRM is whatever
    # it leaves. Drawn blind-first (vscode, thunderbird, writer have the least evidence per task),
    # then round-robin over the rest so the tranche is not itself a new blind spot.
    blind = ["vscode", "thunderbird", "libreoffice_writer"]
    for size in (40, 60, 80):
        dev, pools = [], {k: sorted(v) for k, v in by_app.items()}
        for app in blind:
            take = pools.get(app, [])[: size // (2 * len(blind))]
            dev += take
        i = 0
        order = sorted(pools)
        while len(dev) < size:
            app = order[i % len(order)]
            rest = [t for t in pools[app] if t not in dev]
            if rest:
                dev.append(rest[0])
            i += 1
            if i > 10000:
                break
        out[f"DEV tranche {size} -> CONFIRM"] = [t for v in pools.values() for t in v
                                                 if t not in set(dev)]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=None)
    a = ap.parse_args(argv)
    lock = json.loads(LOCK.read_text())
    alpha = a.alpha or lock["stage3"]["boundaries"]["nominal_alpha"][-1]
    print(f"split calibration — final-look alpha {alpha}, 2 runs/task/arm, lenient rule "
          f"(the frozen design)\n")

    hdr = (f"  {'candidate':42} {'task':>5} {'room':>5} {'noise':>6} "
           + " ".join(f"f={int(f*100)}%".rjust(7) for f in EFFECTS)
           + f" {'no-harm':>8} {'runs':>6} {'giorni':>7}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    rows_out = []
    for name, ids in candidates().items():
        if not ids:
            continue
        rates, sources, _ = baseline_rates(ids)
        room = sum(1 for p in rates if p < 1.0)
        saturated = len(ids) - room
        pw = [power(rates, 2, "lenient", f, alpha) for f in EFFECTS]
        # no-harm is a monotone count over the tasks the baseline already passes
        passing = sum(1 for p in rates if p > 0.5)
        harm = wilson_upper(0, passing)
        runs = len(ids) * 2 * 2
        print(f"  {name:42} {len(ids):>5} {room:>5} {noise_pairs(rates, 2, 'lenient'):>6.1f} "
              + " ".join(f"{x:.2f}".rjust(7) for x in pw)
              + f" {'<='+format(100*harm, '.1f')+'%':>8} {runs:>6} {runs/RUNS_PER_DAY:>7.1f}")
        rows_out.append({"candidate": name, "tasks": len(ids), "with_room": room,
                         "saturated": saturated, "passing": passing,
                         "no_harm_upper_pct": round(100 * harm, 2),
                         "runs": runs, "days": round(runs / RUNS_PER_DAY, 1),
                         **{f"power_f{int(f*100)}": round(p, 3) for f, p in zip(EFFECTS, pw)},
                         "sources": json.dumps(sources)})

    print("\n  `room` = tasks the baseline does not already pass at 1.0. A saturated task cannot "
          "show an\n  effect in either direction, so it carries the cost of a run and none of the "
          "information.")
    print("  `no-harm` = Wilson upper bound on the share of already-passing tasks an arm breaks, "
          "given\n  zero observed. It needs no effect size, which is why it is the deliverable "
          "that survives a\n  small stratum -- and why the comparison above is not only about "
          "power.")

    with (HERE / "tables" / "split_calibration.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0]))
        w.writeheader(); w.writerows(rows_out)
    print(f"\ntable: {HERE / 'tables' / 'split_calibration.csv'}")


if __name__ == "__main__":
    main()
