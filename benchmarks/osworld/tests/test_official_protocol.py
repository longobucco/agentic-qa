import datetime

from benchmarks.osworld import official_protocol as op


def test_prompt_is_upstream_text_with_date_password_and_step_warning():
    p = op.system_prompt(max_steps=100, client_password="password",
                         today=datetime.date(2026, 9, 24))
    assert p.startswith("<SYSTEM_CAPABILITY>")
    assert "* The current date is Thursday, September 24, 2026." in p
    assert "the password of the computer is 'password'" in p
    assert "osworld-public-evaluation" not in p
    assert "[INFEASIBLE]" in p
    assert p.endswith("\n* You have a maximum of 100 steps to complete the task.")


def test_template_has_only_the_two_placeholders():
    import string
    fields = {f for _, f, _, _ in string.Formatter().parse(op.UPSTREAM_PROMPT_TEMPLATE) if f}
    assert fields == {"date", "password"}


def test_final_action_rules():
    assert op.final_action(["all done"], [{"action": "left_click"}]) == "DONE"
    assert op.final_action(["This is [INFEASIBLE] here"], []) == "FAIL"
    assert op.final_action([], [{"action": "fail"}]) == "FAIL"
    assert op.final_action([], [{"actions": [{"action": "key"}, {"action": "fail"}]}]) == "FAIL"
    assert op.final_action([], []) == "DONE"
