#!/usr/bin/env python3
"""Figures for the OSWorld campaign analysis.

Reads `tables/runs_all.csv` (written by campaign_analysis.py) and writes PDF + PNG figures.

    python make_figures.py <runs_all.csv> <out_dir>

Conventions: only *main* campaigns; "informative" runs = SUCCESS/FAILURE verdicts minus runs the
agent CLI never completed (INFRA-CLI / INFRA-API) and no-op passes (PASS-NOOP); see METHODOLOGY.md.
"""
import collections
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SRC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

ARMS = ["sonnet5-old", "astra-old", "sonnet5-new", "astra-new"]
LABEL = {
    "sonnet5-old": "Sonnet 5\n(Phase 1)",
    "astra-old": "GPT-Astra\n(Phase 1)",
    "sonnet5-new": "Sonnet 5\n(official)",
    "astra-new": "GPT-Astra\n(official)",
}
COLOR = {
    "sonnet5-old": "#6baed6",
    "astra-old": "#fdae6b",
    "sonnet5-new": "#08519c",
    "astra-new": "#d94801",
}
plt.rcParams.update({
    "font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
    "axes.titlesize": 10, "figure.dpi": 150, "savefig.bbox": "tight",
})


def save(fig, name):
    fig.savefig(OUT / f"{name}.pdf")
    fig.savefig(OUT / f"{name}.png", dpi=200)
    plt.close(fig)
    print("wrote", name)


d = pd.read_csv(SRC, low_memory=False)
d = d.loc[:, ~d.columns.duplicated()]
d = d[(d["main"] == True) & d["arm"].isin(ARMS + ["astra-old-openbook"])].copy()
rec = d[d["verdict"].isin(["SUCCESS", "FAILURE"])]
inf = rec[~rec["outcome"].isin(["INFRA-CLI", "INFRA-API", "PASS-NOOP"])]


def pct(a, b):
    return 100 * a / b if b else np.nan


# ---- 1. pass rate by arm
fig, ax = plt.subplots(figsize=(6.2, 3.2))
x = np.arange(len(ARMS))
vals = [pct((inf[inf.arm == a].verdict == "SUCCESS").sum(), (inf.arm == a).sum()) for a in ARMS]
recs = [pct((rec[rec.arm == a].verdict == "SUCCESS").sum(), (rec.arm == a).sum()) for a in ARMS]
ax.bar(x, vals, color=[COLOR[a] for a in ARMS], width=0.62)
ax.scatter(x, recs, marker="_", s=420, color="black", zorder=3, label="recorded rate (all SUCCESS/FAILURE)")
for i, a in enumerate(ARMS):
    n = (inf.arm == a).sum()
    ax.text(i, vals[i] + 1.2, f"{vals[i]:.1f}%\n(n={n})", ha="center", fontsize=8)
ax.set_xticks(x, [LABEL[a] for a in ARMS])
ax.set_ylabel("Pass rate over informative runs (%)")
ax.set_ylim(0, 90)
ax.legend(frameon=False, loc="upper left", fontsize=8)
save(fig, "fig_pass_by_arm")

# ---- 2. composition of run directories
cats = [
    ("informative pass", lambda r: (r.verdict == "SUCCESS") & ~r.outcome.isin(["PASS-NOOP"]), "#2ca02c"),
    ("informative fail", lambda r: (r.verdict == "FAILURE") & ~r.outcome.isin(["INFRA-CLI", "INFRA-API"]), "#d62728"),
    ("agent CLI/API aborted", lambda r: r.outcome.isin(["INFRA-CLI", "INFRA-API"]), "#9467bd"),
    ("no-op pass", lambda r: r.outcome == "PASS-NOOP", "#bcbd22"),
    ("evaluator/setup error", lambda r: r.verdict.isin(["EVAL_ERROR", "ENVIRONMENT_ERROR"]), "#7f7f7f"),
    ("no verdict (quota, flake)", lambda r: r.verdict.isna(), "#c7c7c7"),
]
fig, ax = plt.subplots(figsize=(6.4, 3.0))
left = np.zeros(len(ARMS))
for name, fn, col in cats:
    share = np.array([100 * fn(d[d.arm == a]).sum() / (d.arm == a).sum() for a in ARMS])
    ax.barh(range(len(ARMS)), share, left=left, color=col, label=name, height=0.62)
    left += share
ax.set_yticks(range(len(ARMS)), [LABEL[a].replace("\n", " ") for a in ARMS])
ax.invert_yaxis()
ax.set_xlabel("Share of run directories (%)")
ax.legend(frameon=False, fontsize=7, ncol=3, loc="upper center", bbox_to_anchor=(0.45, -0.22))
save(fig, "fig_run_composition")

# ---- 3. pass rate by app bucket
BUCKETS = ["chrome", "gimp", "libreoffice_calc", "libreoffice_impress", "libreoffice_writer", "os", "thunderbird", "vlc", "vscode"]
sel = ["sonnet5-old", "astra-old", "sonnet5-new", "astra-new"]
fig, ax = plt.subplots(figsize=(7.2, 3.4))
w = 0.2
for j, a in enumerate(sel):
    ys, ns = [], []
    for b in BUCKETS:
        g = inf[(inf.arm == a) & (inf.bucket == b)]
        ys.append(pct((g.verdict == "SUCCESS").sum(), len(g)) if len(g) >= 10 else np.nan)
        ns.append(len(g))
    ax.bar(np.arange(len(BUCKETS)) + (j - 1.5) * w, ys, w, color=COLOR[a], label=LABEL[a].replace("\n", " "))
ax.set_xticks(range(len(BUCKETS)), [b.replace("libreoffice_", "LO ") for b in BUCKETS], rotation=25, ha="right")
ax.set_ylabel("Pass rate (%)")
ax.set_ylim(0, 105)
ax.legend(frameon=False, ncol=4, fontsize=7, loc="upper center", bbox_to_anchor=(0.5, 1.13))
save(fig, "fig_bucket")

# ---- 4. failure modes (FAILURE verdicts, informative)
fails = inf[inf.verdict == "FAILURE"]
MODES = [
    ("claimed-done-wrong", "claimed done, checker 0", "#4c72b0"),
    ("declared-fail", "declared FAIL / infeasible", "#dd8452"),
    ("capped", "step/turn cap", "#55a868"),
    ("tool-surface-violation", "harness tool rule", "#c44e52"),
    ("answered-in-text-no-action", "text answer, no action", "#8172b3"),
    ("declined-without-acting", "refusal", "#937860"),
    ("checker-strict-case-only", "checker strictness", "#da8bc3"),
    ("transcript-lost", "transcript lost", "#8c8c8c"),
]
arms_f = [a for a in ARMS]
fig, ax = plt.subplots(figsize=(6.6, 3.1))
left = np.zeros(len(arms_f))
for mode, lab, col in MODES:
    share = np.array([pct((fails[fails.arm == a]["mode"] == mode).sum(), (fails.arm == a).sum()) for a in arms_f])
    ax.barh(range(len(arms_f)), share, left=left, color=col, label=lab, height=0.62)
    left += share
ax.set_yticks(range(len(arms_f)), [LABEL[a].replace("\n", " ") for a in arms_f])
ax.invert_yaxis()
ax.set_xlim(0, 100)
ax.set_xlabel("Share of FAILURE verdicts (%)")
ax.legend(frameon=False, fontsize=7, ncol=3, loc="upper center", bbox_to_anchor=(0.45, -0.22))
save(fig, "fig_failure_modes")

# ---- 5. environment factors
FACT = [("black-desktop", "black desktop"), ("site-blocked", "site blocked"),
        ("missing-precondition", "missing precondition"), ("tool-http-error", "desktop-tool HTTP errors")]
fig, ax = plt.subplots(figsize=(6.2, 3.0))
w = 0.16
for j, (f, lab) in enumerate(FACT):
    ys = [pct((fails[fails.arm == a]["factor"] == f).sum(), (fails.arm == a).sum()) for a in ARMS]
    ax.bar(np.arange(len(ARMS)) + (j - 1.5) * w, ys, w, label=lab)
ax.set_xticks(range(len(ARMS)), [LABEL[a] for a in ARMS])
ax.set_ylabel("Share of FAILURE verdicts (%)")
ax.legend(frameon=False, fontsize=7)
save(fig, "fig_env_factors")

# ---- 6. old -> new task flips
old_arms, new_arms = ["sonnet5-old", "astra-old"], ["sonnet5-new", "astra-new"]
g = inf.groupby(["task_id", "arm"]).agg(p=("verdict", lambda s: (s == "SUCCESS").sum()), n=("verdict", "size")).reset_index()
rows = []
for t, grp in g.groupby("task_id"):
    o, nw = grp[grp.arm.isin(old_arms)], grp[grp.arm.isin(new_arms)]
    if o.n.sum() >= 3 and nw.n.sum() >= 3:
        rows.append((t, o.p.sum() / o.n.sum(), nw.p.sum() / nw.n.sum(), o.n.sum() + nw.n.sum()))
fl = pd.DataFrame(rows, columns=["task", "old", "new", "n"])
rng = np.random.default_rng(1)
fig, ax = plt.subplots(figsize=(4.8, 4.6))
jx, jy = rng.uniform(-0.02, 0.02, len(fl)), rng.uniform(-0.02, 0.02, len(fl))
colr = np.where((fl.old >= 0.66) & (fl.new == 0), "#d62728", np.where((fl.old == 0) & (fl.new >= 0.66), "#2ca02c", "#7f7f7f"))
ax.scatter(fl.old + jx, fl.new + jy, s=12 + fl.n * 1.2, c=colr, alpha=0.6, edgecolor="none")
ax.plot([0, 1], [0, 1], color="black", lw=0.6, ls=":")
ax.set_xlabel("Pass rate, old infrastructure (Phase 1 arms pooled)")
ax.set_ylabel("Pass rate, official infrastructure (both models pooled)")
ax.set_xlim(-0.05, 1.05)
ax.set_ylim(-0.05, 1.05)
nr = int(((fl.old >= 0.66) & (fl.new == 0)).sum())
ng = int(((fl.old == 0) & (fl.new >= 0.66)).sum())
from matplotlib.lines import Line2D
ax.legend(handles=[Line2D([], [], marker="o", ls="", color="#2ca02c", label=f"gain: old 0%, official >=66% ({ng} tasks)"),
                   Line2D([], [], marker="o", ls="", color="#d62728", label=f"regression: old >=66%, official 0% ({nr} tasks)"),
                   Line2D([], [], marker="o", ls="", color="#7f7f7f", label=f"other ({len(fl) - ng - nr} tasks)")],
          frameon=False, fontsize=7.5, loc="upper center", bbox_to_anchor=(0.5, -0.16))
save(fig, "fig_flips")

# ---- 7. wall time and cost, passes vs failures
fig, axs = plt.subplots(1, 2, figsize=(7.4, 3.2))
for ax, col, title, scale in ((axs[0], "wall_s", "Wall time per run (s, log)", "log"), (axs[1], "cost_usd", "Cost per run (USD, log)", "log")):
    data, pos, cols = [], [], []
    for i, a in enumerate(ARMS):
        for k, (v, off) in enumerate((("SUCCESS", -0.17), ("FAILURE", 0.17))):
            s = inf[(inf.arm == a) & (inf.verdict == v)][col].dropna()
            s = s[s > 0]
            data.append(s.values)
            pos.append(i + off)
            cols.append(("#2ca02c" if v == "SUCCESS" else "#d62728"))
    bp = ax.boxplot(data, positions=pos, widths=0.28, showfliers=False, patch_artist=True)
    for patch, c in zip(bp["boxes"], cols):
        patch.set_facecolor(c)
        patch.set_alpha(0.55)
    for m in bp["medians"]:
        m.set_color("black")
    ax.set_yscale(scale)
    ax.set_xticks(range(len(ARMS)), [LABEL[a].replace("\n", "\n") for a in ARMS], fontsize=6.5)
    ax.set_title(title)
from matplotlib.patches import Patch
axs[0].legend(handles=[Patch(color="#2ca02c", alpha=0.55, label="pass"), Patch(color="#d62728", alpha=0.55, label="fail")], frameon=False, fontsize=7)
save(fig, "fig_time_cost")

# ---- 8. run-to-run consistency
fig, ax = plt.subplots(figsize=(6.0, 2.9))
left = np.zeros(len(ARMS))
cnt = {}
for a in ARMS:
    t = inf[inf.arm == a].groupby("task_id").verdict.agg(lambda s: ((s == "SUCCESS").sum(), len(s)))
    t = t[t.map(lambda z: z[1] >= 2)]
    cnt[a] = (sum(1 for p, n in t if p == n), sum(1 for p, n in t if 0 < p < n), sum(1 for p, n in t if p == 0))
for k, (lab, col) in enumerate((("all runs pass", "#2ca02c"), ("mixed", "#ffbf00"), ("all runs fail", "#d62728"))):
    share = np.array([100 * cnt[a][k] / sum(cnt[a]) for a in ARMS])
    ax.barh(range(len(ARMS)), share, left=left, color=col, label=lab, height=0.62)
    left += share
ax.set_yticks(range(len(ARMS)), [LABEL[a].replace("\n", " ") for a in ARMS])
ax.invert_yaxis()
ax.set_xlabel("Share of tasks with at least 2 informative runs (%)")
ax.legend(frameon=False, fontsize=7, ncol=3, loc="upper center", bbox_to_anchor=(0.45, -0.25))
save(fig, "fig_consistency")

# ---- 9. failures by checker family (official arms)
newf = fails[fails.arm.isin(["sonnet5-new", "astra-new"])].copy()
fam = newf["checker"].fillna("(harness)").astype(str).str.replace(r"^\[.*", "(multi-check)", regex=True)
top = fam.value_counts().head(12).index
fig, ax = plt.subplots(figsize=(6.6, 3.6))
for j, a in enumerate(["sonnet5-new", "astra-new"]):
    vals = [(fam[newf.arm == a] == f).sum() for f in top]
    ax.barh(np.arange(len(top)) + (j - 0.5) * 0.38, vals, 0.36, color=COLOR[a], label=LABEL[a].replace("\n", " "))
ax.set_yticks(range(len(top)), list(top))
ax.invert_yaxis()
ax.set_xlabel("Failed runs")
ax.legend(frameon=False, fontsize=8)
save(fig, "fig_checkers")

# ---- 10. infrastructure friction: retry records per arm
rt = d.groupby("arm").infra_attempts.sum().reindex(ARMS + ["astra-old-openbook"])
fig, ax = plt.subplots(figsize=(5.4, 2.8))
ax.bar(range(len(ARMS)), [rt[a] for a in ARMS], color=[COLOR[a] for a in ARMS], width=0.62)
for i, a in enumerate(ARMS):
    ax.text(i, rt[a] + 60, f"{int(rt[a]):,}", ha="center", fontsize=8)
ax.set_xticks(range(len(ARMS)), [LABEL[a] for a in ARMS])
ax.set_ylabel("Aborted attempts recorded\n(`infra_error.json` entries)")
save(fig, "fig_infra_friction")
print("done")
