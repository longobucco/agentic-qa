"""E7: the look boundaries and the power table of X22 stage 3.

    OSW_PHASE3_SUFFIX=pkgfix3c .venv/bin/python -m benchmarks.osworld.gates.calibrate [--write]

The primary test is an exact McNemar on discordant tasks, looked at three times. Nominal alphas
therefore cannot be read off a normal table: they are calibrated by Monte Carlo on the test's own
null distribution, with an O'Brien-Fleming shaped spend (conservative early, so an early peek
costs almost nothing), and then *verified* over a grid of discordance rates, because the
discordance rate is a nuisance parameter the design does not fix.

Per-task baseline rates come from what is already on disk: the Astra column of
campaign_analysis/tables/task_matrix.csv, which is the original-image campaign and the only
per-task Astra distribution available for the whole population. That is an input to a power
calculation, not to the test, so using campaign data here does not touch the confirmatory analysis.

Alternatives are stated as "the arm rescues a fraction f of the tasks the baseline fails", because
that is the shape every arm effect has had so far (X5: e1card flipped 3 of 11 class-F tasks from
0/5 to 5/5 and left the other 8 untouched), not a uniform shift.
"""
import argparse
import csv
import json
import random
from math import comb
from pathlib import Path

from benchmarks.osworld import x22_lock

HERE = Path(__file__).resolve().parent
LOCK = HERE.parent / "x22_campaign_lock.json"
MATRIX = HERE.parent / "campaign_analysis" / "tables" / "task_matrix.csv"
TRIALS = 20000
SEED = 20261112
GRID = (0.05, 0.10, 0.15, 0.20, 0.30, 0.40)   # discordance rates the calibration is verified over
ALPHA = 0.05


def mcnemar_p(b, c):
    """One-sided exact p: P(B >= b | b + c, 1/2). 1.0 when there is no discordance."""
    n = b + c
    if n == 0:
        return 1.0
    return sum(comb(n, i) for i in range(b, n + 1)) / 2 ** n


def obf_spend(fractions, alpha=ALPHA):
    """O'Brien-Fleming style cumulative spend at each information fraction.

    Lan-DeMets with the OBF spending function alpha*2*(1-Phi(z_{alpha/2}/sqrt(t))); approximated
    here by the closed form t -> alpha * t**3, which is monotone, spends almost nothing at t=1/3
    (alpha/27) and all of it at t=1. The exact shape does not matter for validity -- the Monte
    Carlo calibration below enforces the overall rate whatever shape is chosen -- it only sets how
    conservative the early looks are.
    """
    return [alpha * t ** 3 for t in fractions]


def _simulate_null(n_tasks, looks, rate, trials, rng):
    """Max 'crossing opportunity' per trial: the sequence of (b, c) at each look under H0.

    Under H0 a discordant task is equally likely to favour either arm, so each discordant task is a
    fair coin. Concordant tasks carry no information. Returns, per trial, the list of exact p-values
    at the looks.
    """
    out = []
    for _ in range(trials):
        b = c = 0
        ps, nxt = [], 0
        disc = [i for i in range(n_tasks) if rng.random() < rate]
        disc_set = set(disc)
        for i in range(n_tasks):
            if i in disc_set:
                if rng.random() < 0.5:
                    b += 1
                else:
                    c += 1
            while nxt < len(looks) and i + 1 >= looks[nxt]:
                ps.append(mcnemar_p(b, c))
                nxt += 1
        while len(ps) < len(looks):
            ps.append(mcnemar_p(b, c))
        out.append(ps)
    return out


def calibrate(n_tasks, fractions, trials=TRIALS, seed=SEED):
    """Nominal alphas with an OBF spend, calibrated so the overall one-sided rate is <= ALPHA for
    every discordance rate in GRID (the worst case is taken, so the design is conservative)."""
    looks = [max(1, round(f * n_tasks)) for f in fractions]
    target = obf_spend(fractions)
    nominal = []
    for k, cum in enumerate(target):
        # the spend available at this look, given what the earlier looks can already use
        nominal.append(cum if k == 0 else cum - target[k - 1])
    # verify and, if needed, shrink uniformly until the worst-case rate is within ALPHA
    scale, worst = 1.0, None
    for _ in range(40):
        rates = []
        for rate in GRID:
            rng = random.Random(seed)
            sims = _simulate_null(n_tasks, looks, rate, trials, rng)
            rejected = sum(1 for ps in sims
                           if any(p <= nominal[k] * scale for k, p in enumerate(ps)))
            rates.append(rejected / trials)
        worst = max(rates)
        if worst <= ALPHA:
            break
        scale *= 0.8
    return {"looks_at_tasks": looks, "nominal_alpha": [round(a * scale, 6) for a in nominal],
            "shape": "O'Brien-Fleming style, alpha * t**3", "scale_applied": round(scale, 4),
            "worst_case_type_I": round(worst, 4), "verified_over_discordance_rates": list(GRID),
            "trials": trials, "seed": seed, "overall_alpha": ALPHA}


POPULATION_PASS_RATE = 0.719   # Astra, informative runs of the original-image campaign (FINDINGS)


def baseline_rates(task_ids):
    """Per-task baseline pass probability, with a declared source hierarchy.

    Astra's own column first; Sonnet's where Astra never ran (the two campaigns' pass rates are
    71.3% and 72.8%, so Sonnet is a usable proxy for a power calculation and covers 261 tasks
    against Astra's 155); then the population pass rate.

    The fallback is deliberately NOT the mean of the tasks that do have data. In a stratum built by
    excluding tasks named in a failure analysis, the tasks with data are the ones a campaign
    reached and are enriched for passes -- using their mean put every unknown task at 1.0 and drove
    the power estimate to zero, which is a property of the fallback, not of the design.
    """
    rows = {r["task_id"]: r for r in csv.DictReader(MATRIX.open())}

    def cell(tid, col):
        raw = (rows.get(tid, {}).get(col) or "").strip()
        if "/" not in raw:
            return None
        a, b = raw.split("/", 1)
        try:
            a, b = int(a), int(b)
        except ValueError:
            return None
        return a / b if b else None

    rates, sources = [], {"astra": 0, "sonnet": 0, "population": 0}
    for tid in task_ids:
        p = cell(tid, "astra-new_pass/valid")
        if p is not None:
            sources["astra"] += 1
        else:
            p = cell(tid, "sonnet5-new_pass/valid")
            if p is not None:
                sources["sonnet"] += 1
            else:
                p, _ = POPULATION_PASS_RATE, sources.__setitem__(
                    "population", sources["population"] + 1)
        rates.append(p)
    return rates, sources, POPULATION_PASS_RATE


def power(rates, fractions, nominal, runs=2, effects=(0.10, 0.20, 0.30, 0.50),
          trials=4000, seed=SEED):
    """Power of the sequential procedure under 'the arm rescues a fraction f of failed tasks'.

    Gain is measured on the lenient definition the lock fixes (solved = at least 1 of 2 runs),
    which is what the primary test uses.
    """
    n = len(rates)
    looks = [max(1, round(f * n)) for f in fractions]
    out = {}
    for f in effects:
        rng = random.Random(seed)
        wins = 0
        for _ in range(trials):
            b = c = 0
            ps, nxt = [], 0
            for i, p0 in enumerate(rates):
                # the arm rescues a failed task with probability f; otherwise same rate
                p1 = 1.0 if (p0 < 1.0 and rng.random() < f) else p0
                s0 = any(rng.random() < p0 for _ in range(runs))
                s1 = any(rng.random() < p1 for _ in range(runs))
                if s1 and not s0:
                    b += 1
                elif s0 and not s1:
                    c += 1
                while nxt < len(looks) and i + 1 >= looks[nxt]:
                    ps.append(mcnemar_p(b, c))
                    nxt += 1
            while len(ps) < len(looks):
                ps.append(mcnemar_p(b, c))
            if any(p <= nominal[k] for k, p in enumerate(ps)):
                wins += 1
        out[f"rescues_{int(f * 100)}pct_of_failed"] = round(wins / trials, 3)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write the boundaries into the lock")
    a = ap.parse_args(argv)
    lock = json.loads(LOCK.read_text())
    stage3 = lock["stage3"]
    ids = stage3["strata"][stage3["primary_stratum"]]
    fractions = stage3["looks_at_task_fractions"]
    print(f"stage 3 primary stratum: {stage3['primary_stratum']}, {len(ids)} tasks, "
          f"{stage3['runs_per_task']} runs per task, looks at {fractions}")

    bounds = calibrate(len(ids), fractions)
    print(f"\nlooks after {bounds['looks_at_tasks']} tasks complete on both arms")
    print(f"nominal one-sided alpha per look: {bounds['nominal_alpha']}")
    print(f"worst-case overall type I over discordance rates {bounds['verified_over_discordance_rates']}: "
          f"{bounds['worst_case_type_I']} (target {ALPHA}); shrink factor applied "
          f"{bounds['scale_applied']}")

    rates, sources, fallback = baseline_rates(ids)
    print(f"\nper-task baseline rates by source: {sources} (fallback = population rate {fallback})")
    pw = power(rates, fractions, bounds["nominal_alpha"], runs=stage3["runs_per_task"])
    print("power of the sequential procedure:")
    for k, v in pw.items():
        print(f"  {k:34} {v:.3f}")
    bounds["power"] = pw
    bounds["baseline_rate_sources"] = sources
    bounds["baseline_rates_fallback"] = fallback

    if not a.write:
        print("\n(dry run: pass --write to freeze these into the lock)")
        return
    if stage3.get("boundaries") is not None:
        raise SystemExit("boundaries are already frozen in the lock and are never recomputed")
    stage3["boundaries"] = bounds
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"\nfrozen into {LOCK.name}")


if __name__ == "__main__":
    main()
