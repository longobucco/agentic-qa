"""WebArena runner: drive the Steel agent-browser against the hosted stack, then CAPTURE the
deterministic-judge inputs (final URL + each program_html target's element text) into
result.json while the browser/stack are still up. `evaluate.py` scores those with pure
functions — no LLM. Serial only (shared stack + per-task reset); see benchmark.py.
"""
import subprocess

from benchmarks.webarena import config, tasks
from benchmarks.webarena.prompts import agent_docker_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io
from core.steel import steel_container, new_name


def _ab(cname, *args, timeout=30):
    r = subprocess.run(["docker", "exec", cname, "agent-browser", *args],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip()


def _template(url):
    for ph, target in config.SITE_URLS.items():
        if target:
            url = url.replace(ph, target)
    return url


def _capture_program_html(cname, spec, final_url):
    """For each program_html target: go to its url ('last' = the page the agent ended on),
    eval the locator, and record the text. Pure JS locators only (`document...`/empty);
    `func:` locators are recorded as '' (unsupported) and will fail-closed in the judge."""
    contents = []
    for target in spec.get("program_html", []):
        url = target.get("url", "last")
        if url and url != "last":
            _ab(cname, "open", _template(url))
        loc = (target.get("locator") or "").strip()
        if not loc:
            expr = "document.body.innerText"
        elif loc.startswith("document"):
            expr = loc
        else:
            contents.append("")          # func:/unsupported locator
            continue
        contents.append(_ab(cname, "eval", expr))
    return contents


def run(task, *, env, out, refs=None, dry=False):
    if dry:
        cmd = build_claude_cmd(agent_docker_prompt(task, new_name("waab")),
                               model=config.MODEL or None, max_turns=config.MAX_TURNS)
        print("DRY-RUN command:\n ", preview(cmd))
        return None

    spec = task.get("eval", {})
    with steel_container("waab") as cname:
        cmd = build_claude_cmd(
            agent_docker_prompt(task, cname),
            model=config.MODEL or None,
            max_turns=config.MAX_TURNS,
        )
        text = run_claude(cmd, timeout=config.TASK_TIMEOUT)
        answer = extract_answer(text)
        # capture deterministic-judge inputs while the stack/browser are still live
        final_url = _ab(cname, "eval", "location.href")
        program_html = (_capture_program_html(cname, spec, final_url)
                        if "program_html" in spec.get("eval_types", []) else None)

    results_io.write_output(out, text)
    record = {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "intent": task["intent"],
        "answer": answer,
        "final_url": final_url,
    }
    if program_html is not None:
        record["program_html_contents"] = program_html
    results_io.write_result(out, record)
    return answer
