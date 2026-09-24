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


def test_a_clean_run_has_no_api_error_status():
    assert astra_common.api_error_status({"errors": None}) is None


def test_rate_limit_rec_uses_the_rate_limited_outcome_for_429():
    rec = astra_common.rate_limit_rec({"id": "t1"}, 429)
    assert rec["outcome"] == "RATE_LIMITED"


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
