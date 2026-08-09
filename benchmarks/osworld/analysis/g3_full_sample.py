"""G3-full: run every eligible task, not just G3's stratified k=40 -> tighter Wilson CI on the
benchmark-level pass-rate (gap-research-plan.md, G3-full).

Gate decided 2026-08-09: k=40 gives a 95% CI half-width of ~13.4pp (measured at k=28, partial
G3 data). k=260 would narrow that to ~5.5pp, but at 6.5x the run count -- fixed as a step right
after G3 anyway, not deferred on the k=40 result.

205 tasks clear g3_sample's exclusions (unscorable, broken setup, unroutable getters, missing
postconfig init) -- same population sample() draws its stratified 40 from, here uncapped. 40 of
those already have full G3 runs; this module targets only the other 165.

Run: python -m benchmarks.osworld.analysis.g3_full_sample
"""
from benchmarks.osworld.analysis.g3_sample import full, sample


def remaining():
    """The 165 eligible tasks not already covered by G3's stratified sample."""
    return sorted(set(full()) - set(sample()))


def _main():
    ids = remaining()
    print(f"{len(ids)} additional tasks ({len(ids) * 3} runs at N=3):\n")
    print("python -m benchmarks.osworld.run --ids " + " ".join(ids) + " --runs 3")


if __name__ == "__main__":
    _main()
