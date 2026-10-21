"""Unit tests for runners/astra_common.py's api_error_status/rate_limit_rec, used by gpt_astra.py:
  python -m benchmarks.osworld.tests.test_astra_common
"""
from benchmarks.osworld.runners import astra_common


def test_a_429_in_errors_is_a_real_rate_limit():
    meta = {"errors": [{"message": "429 Too Many Requests"}]}
    assert astra_common.api_error_status(meta) == 429


def test_a_quota_message_on_stderr_only_is_still_a_rate_limit():
    assert astra_common.api_error_status({}, stderr="you have exceeded your quota") == 429


def test_a_revoked_refresh_token_is_auth_revoked_not_a_rate_limit():
    """Confirmed live 2026-09-15: a Codex CLI session revocation ("Your access token could not
    be refreshed because your refresh token was revoked") produces agent_num_turns=0 exactly
    like a rate limit, but was NOT caught by the rate-limit patterns -- 4 runs across 2 tasks
    were scored as genuine FAILURE before this was caught. Must be classified distinctly:
    backing off and retrying (the right response to a real rate limit) does nothing for this."""
    meta = {"errors": [{"message": "Your access token could not be refreshed because your "
                                   "refresh token was revoked. Please log out and sign in "
                                   "again."}]}
    assert astra_common.api_error_status(meta) == "AUTH_REVOKED"


def test_a_401_unauthorized_on_stderr_is_auth_revoked():
    stderr = "ERROR: failed to refresh available models: unexpected status 401 Unauthorized"
    assert astra_common.api_error_status({}, stderr=stderr) == "AUTH_REVOKED"


# The errors Codex 0.153.4 reported on 3ce045a0 run_2 (2026-09-30), when the harness host lost
# DNS mid-run: the turn ended in error after one turn and was scored FAILURE.
_NETWORK_ERRORS = [
    "Reconnecting... 2/5 (request timed out)",
    "Reconnecting... 3/5 (stream disconnected before completion: failed to lookup address "
    "information: nodename nor servname provided, or not known)",
    "Reconnecting... waiting for network (Connection failed: error sending request)",
]


def test_a_turn_that_died_on_the_network_is_a_network_error_not_a_verdict():
    meta = {"is_error": True, "errors": _NETWORK_ERRORS}
    assert astra_common.api_error_status(meta) == "NETWORK_ERROR"


def test_an_idle_websocket_timeout_that_ended_the_turn_is_a_network_error():
    meta = {"is_error": True, "errors": [
        "Reconnecting... 2/5 (stream disconnected before completion: idle timeout waiting for "
        "websocket)"]}
    assert astra_common.api_error_status(meta) == "NETWORK_ERROR"


def test_reconnects_in_a_turn_that_went_on_to_finish_are_not_an_error():
    assert astra_common.api_error_status({"is_error": False, "errors": _NETWORK_ERRORS}) is None


def test_a_rate_limit_wins_over_network_noise():
    meta = {"is_error": True, "errors": _NETWORK_ERRORS + ["429 Too Many Requests"]}
    assert astra_common.api_error_status(meta) == 429


def test_rate_limit_rec_files_a_network_error_as_an_infra_flake():
    rec = astra_common.rate_limit_rec({"id": "t1"}, "NETWORK_ERROR")
    assert rec["outcome"] == "INFRA_FLAKE"
    assert rec["quota_exhausted"] is False


def test_a_clean_run_has_no_api_error_status():
    assert astra_common.api_error_status({"errors": None}) is None


def test_rate_limit_rec_uses_the_rate_limited_outcome_for_429():
    rec = astra_common.rate_limit_rec({"id": "t1"}, 429)
    assert rec["outcome"] == "RATE_LIMITED"


def test_rate_limit_rec_marks_codexs_usage_limit_as_quota_exhausted():
    text = "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro)"
    assert astra_common.rate_limit_rec({"id": "t1"}, 429, text)["quota_exhausted"] is True
    assert astra_common.rate_limit_rec({"id": "t1"}, 429, "HTTP 429")["quota_exhausted"] is False


def test_rate_limit_rec_uses_a_distinct_outcome_for_auth_revoked():
    rec = astra_common.rate_limit_rec({"id": "t1"}, "AUTH_REVOKED")
    assert rec["outcome"] == "AUTH_ERROR"


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
