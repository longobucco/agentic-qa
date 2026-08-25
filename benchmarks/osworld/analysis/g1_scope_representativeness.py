"""G1: is the in-scope subset representative of the full 369? Offline, no sandbox.

We run only tasks whose apps are all in the current operational scope
(config.SUPPORTED_APPS + config.ALWAYS_PRESENT_CAPABILITIES). If that in-scope set
differs systematically from the excluded tasks on difficulty or evaluator quality,
a pass-rate on them does not extrapolate to OSWorld. G1 tests that.

Split is operational -- what the harness runs today: 255 in / 114 out. The 22
excluded only by related_apps spelling (see g0) are a metadata issue, not missing
support, and are reported apart, not mixed into "out".

App is NOT tested: scope was chosen BY app, so its distribution differs by
construction (tautological). The informative dimensions are the ones that skew a
pass-rate independently of app -- evaluator status/strength (from g0), infeasible
presence (its own capability, tested standalone, not folded into status), setup
complexity (config steps), difficulty proxies (instruction length, multi-app).

Tests: chi-square + Cramer's V for categorical, Mann-Whitney U + rank-biserial for
numeric, Benjamini-Hochberg across the family. At n~369 effect size matters more
than p -- both are reported.

Run: python -m benchmarks.osworld.analysis.g1_scope_representativeness
"""
import json

from scipy import stats

from benchmarks.osworld import config
from benchmarks.osworld.analysis import g0_evaluator_audit as g0

# conventional effect-size bands (Cohen): applied to Cramer's V and |rank-biserial|
BANDS = ((0.1, "negligible"), (0.3, "small"), (0.5, "medium"), (1.01, "large"))


def _band(v):
    return next(label for hi, label in BANDS if v < hi)


# capability of each non-app/unsupported tag, judged against the installed image
# (docker/Dockerfile.osworld). related_apps mixes apps with capabilities: terminal =
# shell, always present; os = partial (file ops work, gio trash/notify-send don't);
# browser = chrome; libreoffice = the installed suite. picard/ubuntu_media_player are
# genuinely absent.
CAP_PRESENT = {"terminal", "libreoffice", "browser"}
CAP_ABSENT = {"picard", "ubuntu_media_player"}
CAP_PARTIAL = {"os", "pdf", "image"}


def scope_audit():
    """Classify excluded tasks by whether the required capability is actually in the image,
    to catch tasks excluded by a literal tag match despite being runnable. 'erroneous' =
    every unsupported tag is a present capability -- still needs live validation before
    promotion. Checked against the current operational scope, so already-promoted tasks
    don't keep re-appearing here."""
    sup = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    tasks = g0._load_tasks()
    out = [t for t in tasks if not (set(t.get("related_apps") or []) <= sup)]

    buckets = {"spelling": [], "erroneous": [], "needs_check": [], "correct": []}
    for t in out:
        norm = {g0._norm_app(a) for a in (t.get("related_apps") or [])}
        if norm <= sup:
            buckets["spelling"].append(t)
        elif (unsup := norm - sup) & CAP_ABSENT:
            buckets["correct"].append(t)
        elif unsup <= CAP_PRESENT:
            buckets["erroneous"].append(t)
        else:
            buckets["needs_check"].append(t)
    return buckets


def validate_terminal_candidates(controller_url):
    """Live check (needs a sandbox, no agent): run each likely-erroneous task's config
    through the real setup path. setup_error None -> prepares cleanly -> promote to
    SUPPORTED_APPS. Non-None -> keep excluded."""
    from benchmarks.osworld.env import sandbox as sb
    from benchmarks.osworld.env.controller import Controller
    ctrl = Controller(controller_url)
    out = []
    for t in scope_audit()["erroneous"]:
        err = sb._run_config(ctrl, t)
        out.append({"id": t["id"], "apps": t.get("related_apps"), "setup_error": err})
        print(out[-1])
    return out


def _split(normalized=False):
    """normalized=False: literal match against the current operational scope
    (SUPPORTED_APPS + promoted capabilities -- 260/109, what the harness runs
    today). normalized=True: additionally fold in the 22 spelling variants, a
    robustness check that the skew is not an artifact of those 22 tasks."""
    tasks = g0._load_tasks()
    sup = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    key = (lambda t: {g0._norm_app(a) for a in (t.get("related_apps") or [])}) if normalized \
        else (lambda t: set(t.get("related_apps") or []))
    ins = [t for t in tasks if key(t) <= sup]
    out = [t for t in tasks if t not in ins]
    spelling = [t for t in out
                if {g0._norm_app(a) for a in (t.get("related_apps") or [])} <= sup]
    return ins, out, spelling


def _cramers_v(chi2, n):
    # 2xk table: min(rows,cols)-1 = 1, so V = sqrt(chi2 / n)
    return (chi2 / n) ** 0.5


def _rank_biserial(u, n1, n2):
    return abs(1 - 2 * u / (n1 * n2))


def _chi2_dim(name, ins, out, key, levels):
    """key(task) -> a level; build the 2xk in/out table over `levels` and test."""
    def counts(g):
        c = {lv: 0 for lv in levels}
        for t in g:
            k = key(t)
            if k in c:
                c[k] += 1
        return c
    ci, co = counts(ins), counts(out)
    # drop levels absent from both groups so chi-square stays defined
    kept_levels = [lv for lv in levels if ci[lv] + co[lv] > 0]
    table = [[ci[lv] for lv in kept_levels], [co[lv] for lv in kept_levels]]
    chi2, p, _, _ = stats.chi2_contingency(table)
    n = len(ins) + len(out)
    v = _cramers_v(chi2, n)
    dist_i = {lv: f"{100*ci[lv]/len(ins):.0f}%" for lv in kept_levels}
    dist_o = {lv: f"{100*co[lv]/len(out):.0f}%" for lv in kept_levels}
    return {"dim": name, "test": "chi2", "p": p, "effect": v, "effect_name": "Cramer's V",
            "in": dist_i, "out": dist_o}


def _mwu_dim(name, ins, out, val):
    xi = [val(t) for t in ins]
    xo = [val(t) for t in out]
    u, p = stats.mannwhitneyu(xi, xo, alternative="two-sided")
    r = _rank_biserial(u, len(xi), len(xo))
    med = lambda xs: sorted(xs)[len(xs) // 2]
    return {"dim": name, "test": "MWU", "p": p, "effect": r, "effect_name": "rank-biserial",
            "in": {"median": med(xi)}, "out": {"median": med(xo)}}


def run(func_classes=None, *, normalized=False):
    classes = func_classes or g0.classify_all()
    ins, out, spelling = _split(normalized)
    normal = lambda g: [t for t in g if g0.task_status(t, classes) == "normal"]

    results = [
        _chi2_dim("evaluator status", ins, out,
                  lambda t: g0.task_status(t, classes), ["normal", "infeasible", "broken"]),
        # standalone, not a level of "evaluator status" -- infeasible tests a distinct
        # capability, checked on its own
        _chi2_dim("infeasible presence", ins, out,
                  lambda t: "infeasible" if g0.task_status(t, classes) == "infeasible" else "other",
                  ["other", "infeasible"]),
        # strength is only defined on normal tasks -> restrict both groups to normal
        _chi2_dim("evaluator strength (normal only)", normal(ins), normal(out),
                  lambda t: g0.task_strength(t, classes), ["strong", "medium", "weak"]),
        _chi2_dim("multi-application", ins, out,
                  lambda t: "multi" if len(t.get("related_apps") or []) > 1 else "single",
                  ["single", "multi"]),
        _mwu_dim("config steps", ins, out, lambda t: len(t.get("config") or [])),
        _mwu_dim("instruction length (words)", ins, out, lambda t: len(t["instruction"].split())),
    ]

    # Benjamini-Hochberg across the family
    qs = stats.false_discovery_control([r["p"] for r in results], method="bh")
    for r, q in zip(results, qs):
        r["q"] = q

    return {"n_in": len(ins), "n_out": len(out), "n_spelling": len(spelling), "results": results}


def _print(rep, title):
    print(f"=== {title}: in-scope {rep['n_in']} / out-of-scope {rep['n_out']} "
          f"({rep['n_spelling']} spelling-only) ===")
    print(f"{'dimension':<34}{'test':<6}{'q(BH)':<10}{'effect':<24}sig")
    print("-" * 92)
    for r in rep["results"]:
        eff = f"{r['effect']:.2f} {r['effect_name']} ({_band(r['effect'])})"
        sig = "*" if r["q"] < 0.05 else " "
        print(f"{r['dim']:<34}{r['test']:<6}{r['q']:<10.4f}{eff:<24}{sig}")
        print(f"{'':<34}in : {r['in']}")
        print(f"{'':<34}out: {r['out']}")


def _main():
    classes = g0.classify_all()
    print("app dimension not tested: differs by construction (scope chosen by app)\n")
    _print(run(classes), "operational split (current scope: apps + capabilities)")
    print()
    _print(run(classes, normalized=True), "robustness: normalized split (folds in 22 spelling)")
    print("\n* q<0.05 after Benjamini-Hochberg. Read effect size, not just q: at "
          "n~369 tiny gaps reach significance. A dimension significant AND "
          "non-negligible in BOTH splits is a robust skew, not a spelling artifact.")

    print("\n=== scope audit: are excluded tasks really unsupported? ===")
    b = scope_audit()
    print(f"  {len(b['spelling'])} spelling-only (app supported, metadata bug)")
    print(f"  {len(b['erroneous'])} LIKELY ERRONEOUS (capability present; needs live check to promote)")
    print(f"  {len(b['needs_check'])} needs live check (partial capability, mostly os)")
    print(f"  {len(b['correct'])} correctly excluded (app genuinely not installed)")
    for t in b["erroneous"]:
        print(f"    erroneous: {t['id']}  {t.get('related_apps')}")


if __name__ == "__main__":
    _main()
