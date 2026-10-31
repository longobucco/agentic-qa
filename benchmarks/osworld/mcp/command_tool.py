"""run_command: the declared code arm's second tool (arms.ARM_TOOLS). One bash command, executed in
the guest through the controller's /execute (the same route the computer tool uses), never on
the harness host. A call is one step of the same budget as `computer` (ComputerSession.steps_used).
Output is returned as text (exit code, stdout, stderr, each capped); every call is appended to the
run's command log (OSW_COMMAND_LOG) for the adoption analysis."""
import json
import time

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 110      # below the guest server's own 120 s limit, so ours fires first
MAX_CHARS = 8000


def _cap(text):
    text = text or ""
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n[... {len(text) - MAX_CHARS} more characters cut]"


def _log(path, rec):
    if not path:
        return
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def run_command(session, ctrl, command, timeout=DEFAULT_TIMEOUT_S, *, log_path="", clock=time.monotonic):
    if session.steps_used >= session.max_steps:
        return (f"Step limit reached ({session.max_steps}/{session.max_steps}). "
                "No further actions are executed; stop now.")
    session.steps_used += 1
    session._screenshot_seen = True
    step = session.steps_used
    if not isinstance(command, str) or not command.strip():
        return f"Error: command must be a non-empty string\n{session._reminder()}"
    try:
        t = max(1, min(int(timeout), MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    t0 = clock()
    try:
        rec = ctrl.run(["timeout", "--kill-after=5", str(t), "bash", "-c", command], timeout=t + 15)
    except Exception as e:
        rec = {"status": "error", "message": f"{type(e).__name__}: {e}"}
    seconds = round(clock() - t0, 2)
    code = rec.get("returncode")
    _log(log_path, {"step": step, "command": command, "timeout": t, "returncode": code,
                    "status": rec.get("status"), "seconds": seconds,
                    "stdout_chars": len(rec.get("output") or ""),
                    "stderr_chars": len(rec.get("error") or "")})
    if rec.get("status") != "success":
        body = f"Error: {rec.get('message') or 'command failed'}"
    elif code == 124:
        body = (f"Timed out after {t} s (exit code 124)\nstdout:\n{_cap(rec.get('output'))}\n"
                f"stderr:\n{_cap(rec.get('error'))}")
    else:
        body = (f"exit code: {code}\nstdout:\n{_cap(rec.get('output'))}\n"
                f"stderr:\n{_cap(rec.get('error'))}")
    return f"{body}\n{session._reminder()}"
