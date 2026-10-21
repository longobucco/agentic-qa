"""A metric that computes an exact match through floating point can land a hair under 1.0
(compare_audios returned 0.9999999999923035 on 778efd0a, 3 runs filed as FAILURE). Only float
noise is forgiven: a genuinely partial score stays a failure."""
from benchmarks.osworld.env.osworld_eval import reward_to_verdict


def test_float_noise_under_one_is_a_success():
    assert reward_to_verdict(0.9999999999923035) == "SUCCESS"
    assert reward_to_verdict(1.0) == "SUCCESS"


def test_partial_scores_stay_failures():
    assert reward_to_verdict(0.9996001599360256) == "FAILURE"
    assert reward_to_verdict(0.91) == "FAILURE"
    assert reward_to_verdict(None) == "FAILURE"
