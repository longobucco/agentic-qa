"""Unit tests for g0_replication_fidelity.py (offline, no sandbox):
  python -m benchmarks.osworld.tests.test_g0_replication_fidelity
"""
from benchmarks.osworld.analysis import g0_replication_fidelity as g0r


def test_fingerprint_matches_pinned_hash():
    """If this fails, desktop_env's evaluate() changed since the last audit -- every entry in
    KNOWN_DIVERGENCES needs re-verifying by hand before the report can be trusted."""
    assert g0r.fingerprint()["match"] is True


def test_known_divergences_still_hold():
    """Both halves of every recorded divergence must still be true: upstream's source still
    has the bug shape, and our own code still avoids it. Either flipping is worth knowing."""
    for d in g0r.KNOWN_DIVERGENCES:
        assert d["upstream_still_has_bug"]() is True, d["id"]
        assert d["our_behavior_holds"]() is True, d["id"]


def test_coverage_returns_well_formed_report():
    cov = g0r.coverage()
    assert isinstance(cov["missing_getters"], list)
    assert isinstance(cov["missing_metrics"], list)
    for a in cov["affected_tasks"]:
        assert a["id"] and a["symbols"]


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
