"""Open-book OSWorld Verified campaign for GPT Astra: tasks closed_book.classify() defers as
open-book (URL in instruction, app==chrome, or a download/googledrive/login setup step), run
against the controlled fixture gateway (openbook_proxy/, env/guest_proxy.py, env/host_proxy.py)
instead of the live Internet. See docs/g_astra_open_book_runner_implementation.md.

Deliberately its own runner, not a flag on gpt_astra.py: its outputs, lock file, and result tree
must never be combined with agent_computer_astra (the spec's own non-goal). Shares the Codex/
Astra-specific telemetry, rate-limit detection and provenance primitives with gpt_astra.py via
runners/astra_common.py -- everything below this line is open-book-specific orchestration:
task-scoped preflight, the network_provenance record, and routing scoring through the host-side
proxy for the two getters that make their own network calls outside the sandbox.
"""
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld import config, open_book_preflight, tasks
from benchmarks.osworld.env import host_proxy, osworld_eval
from benchmarks.osworld.openbook_proxy.bundle import MOCK_ACCESS_TOKEN
from benchmarks.osworld.prompts import agent_prompt
from benchmarks.osworld.runners import astra_common
from benchmarks.osworld.runners import common as runner_common
from benchmarks.osworld.runners.agent_computer import (
    _annotate_incidental, _bounded, _capture_eval_state, _environment_error_rec,
)
from core import results as results_io
from core.agent_loop import extract_answer, preview
from core.codex_loop import build_codex_cmd, run_codex_meta

_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "astra_openbook_manifest.json"
_HOST_PROXY_TAGS = {"external_live_getter", "external_oauth_service"}
# Detected in every persisted artifact before it's kept -- see _scan_for_secrets. The mock Drive
# token is an internal fixture-gateway secret (drive_mock.py) that must never appear in an agent-
# visible artifact; a hit here means the mock leaked into something the agent could read, not a
# real credential, but the failure mode (an artifact silently ships something it shouldn't) is
# the one the spec's redaction requirement exists to catch.
_SECRET_PATTERNS = (
    re.compile(re.escape(MOCK_ACCESS_TOKEN)),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)

# Re-exported for test patchability (patch.object(gpt_astra_openbook, "_codex_version", ...)),
# same convention as gpt_astra.py.
_codex_version = astra_common.codex_version

_manifest_tags_cache = None


def _proxy_tags_for(task_id):
    global _manifest_tags_cache
    if _manifest_tags_cache is None:
        try:
            data = json.loads(_MANIFEST_PATH.read_text())
            _manifest_tags_cache = {t["task_id"]: set(t.get("proxy_tags") or [])
                                    for t in data["tasks"]}
        except (OSError, json.JSONDecodeError, KeyError):
            _manifest_tags_cache = {}
    return _manifest_tags_cache.get(task_id, set())


def preflight():
    open_book_preflight.campaign_check()


def _provenance(task, ctrl, started_at, codex_ver, session_id=None, extra=None):
    return astra_common.provenance_astra(
        task, ctrl, started_at, codex_ver, model=config.ASTRA_MODEL,
        reasoning_effort=config.ASTRA_REASONING_EFFORT, session_id=session_id, extra=extra)


def _environment_error(task, out, ctrl, started_at, codex_ver, reason):
    rec = _environment_error_rec(task, reason)
    rec["result"]["provenance"] = _provenance(task, ctrl, started_at, codex_ver)
    results_io.write_result(out, rec["result"])
    results_io.write_eval(out, rec["eval"])


def _evaluate_with_retry(url, task, action_history, cache_dir, *, use_proxy):
    """Same transient-proxy retry policy as runners/common._evaluate_with_retry, reused via its
    constants rather than duplicated, but calling evaluate_official with use_proxy -- the shared
    helper has no such parameter (closed-book/verify-replan never need one)."""
    import time
    last_err = None
    for attempt in range(runner_common._EVAL_RETRY_ATTEMPTS):
        try:
            return osworld_eval.evaluate_official(
                url, task, action_history, cache_dir=cache_dir, use_proxy=use_proxy)
        except runner_common._TRANSIENT_EVAL_ERRORS as e:
            last_err = e
            if attempt < runner_common._EVAL_RETRY_ATTEMPTS - 1:
                time.sleep(runner_common._EVAL_RETRY_BASE_DELAY_S * (3 ** attempt))
    raise last_err


def _score_openbook(ctrl, task, answer, out, bundle_dir, proxy_tags):
    """Same shape as runners/common._score, plus: route the evaluator through the host-side
    proxy (scoped to just this call, via host_proxy.scoped_env) when the task's manifest tags say
    its evaluator makes its own network calls outside the sandbox (get_cloud_file /
    get_googledrive_file) -- see build_astra_openbook_manifest.py's proxy_tags."""
    url = ctrl.base_url if ctrl else config.CONTROLLER_URL
    reward, err = None, None
    gold_dir = tempfile.mkdtemp(prefix="osw_openbook_gold_")
    gold_sha256 = {}
    needs_host_proxy = bool(proxy_tags & _HOST_PROXY_TAGS)
    if url:
        action_history = runner_common._action_history(answer)
        try:
            if needs_host_proxy:
                with host_proxy.host_proxy(
                    bundle_dir, needs_drive_mock="external_oauth_service" in proxy_tags,
                ) as handle, host_proxy.scoped_env(handle):
                    reward = _evaluate_with_retry(
                        url, task, action_history, gold_dir, use_proxy=True)
            else:
                reward = _evaluate_with_retry(
                    url, task, action_history, gold_dir, use_proxy=True)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        finally:
            gold_sha256 = osworld_eval.hash_gold_artifacts(gold_dir, task)
            shutil.rmtree(gold_dir, ignore_errors=True)
    if reward is not None:
        return {"verdict": osworld_eval.reward_to_verdict(reward), "reward": reward,
                "reason": f"official {task.get('evaluator', {}).get('func')} -> {reward:.2f}",
                "source": "official", "gold_sha256": gold_sha256}
    from benchmarks.osworld import evaluate
    rec = {**evaluate.osworld_check(task, answer, None, out), "source": "offline_fallback",
           "gold_sha256": gold_sha256}
    if err:
        rec["reason"] = f"{rec['reason']} (official eval errored: {err})"
    return rec


def _network_provenance(ctrl, bundle_dir, manifest_sha256, proxy_tags):
    guest_misses = None
    if ctrl is not None:
        try:
            from benchmarks.osworld.env import guest_proxy
            guest_misses = guest_proxy.misses(ctrl)
        except Exception as e:
            guest_misses = [{"kind": "misses_unreadable", "error": f"{type(e).__name__}: {e}"}]
    return {
        "network_mode": "controlled_snapshot",
        "snapshot_bundle_sha256": manifest_sha256,
        "fixture_namespace": str(bundle_dir),
        "proxy_tags": sorted(proxy_tags),
        "guest_proxy_misses": guest_misses,
    }


def _scan_for_secrets(out):
    """Redaction scan over every text artifact this run wrote, before the run is considered
    finished. A hit here means something that should never leave the fixture gateway (the mock
    Drive token today) reached an agent-visible file -- see the module docstring."""
    hits = []
    for path in out.glob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                hits.append({"file": path.name, "pattern": pattern.pattern})
    return hits


def run(task, *, env, out, refs=None, dry=False):
    started_at = datetime.now(timezone.utc).isoformat()
    ctrl = getattr(env, "browser", None)
    controller_url = ctrl.base_url if ctrl else config.CONTROLLER_URL
    codex_ver = _codex_version()
    setup_error = getattr(env, "setup_error", None)
    if setup_error and not dry:
        # Covers both an ordinary provisioning failure AND env.sandbox's own task_check call
        # (missing fixture / not-open-book / guest proxy failure) -- either way, no Codex
        # invocation happens past this point.
        _environment_error(task, out, ctrl, started_at, codex_ver, setup_error)
        open_book_preflight.write_preflight_json(out, {"ready": False, "reason": setup_error})
        return ""

    check = open_book_preflight.task_check(task)
    open_book_preflight.write_preflight_json(out, check)
    if not check["ready"] and not dry:
        # Reachable when run() is invoked directly against a hand-built env (dry runs, tests)
        # rather than through osworld_openbook_environment, which already calls task_check
        # itself before provisioning -- kept as an independent guarantee either path takes.
        _environment_error(task, out, ctrl, started_at, codex_ver, check["reason"])
        return ""

    bundle_dir = Path(check["bundle_dir"]) if check["bundle_dir"] else None
    proxy_tags = _proxy_tags_for(task["id"])

    workdir = Path(tempfile.mkdtemp(prefix="osw-astra-openbook-"))
    cmd = build_codex_cmd(
        agent_prompt(task), model=config.ASTRA_MODEL, cwd=workdir,
        controller_url=controller_url, reasoning_effort=config.ASTRA_REASONING_EFFORT or None,
    )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        shutil.rmtree(workdir, ignore_errors=True)
        return None

    try:
        meta = run_codex_meta(cmd, timeout=config.TASK_TIMEOUT)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    transcript = out / "conversation.jsonl"
    transcript.write_text(meta.pop("raw", ""))
    stderr = meta.pop("stderr", "")
    if stderr:
        (out / "codex_stderr.txt").write_text(stderr)
    audit = astra_common.write_tool_audit(out, meta.pop("events", None))
    text = meta.get("result", "")
    answer = extract_answer(text)
    clean_finish = bool(answer) and not meta.get("is_error", False)
    results_io.write_output(out, text)
    telemetry = astra_common.telemetry(meta, stderr)
    telemetry["agent_clean_finish"] = clean_finish
    provenance = _provenance(task, ctrl, started_at, codex_ver, session_id=meta.get("session_id"))
    transcript_bytes = transcript.stat().st_size
    trace = {"transcript_saved": transcript_bytes > 0, "transcript_bytes": transcript_bytes,
             "contaminated": audit["contaminated"], "contamination_evidence": audit["evidence"]}

    network_provenance = _network_provenance(
        ctrl, bundle_dir, check.get("manifest_sha256"), proxy_tags)
    (out / "network_provenance.json").write_text(json.dumps(network_provenance, indent=2))

    secret_hits = _scan_for_secrets(out)
    if secret_hits:
        results_io.write_result(out, {
            "id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": answer,
            "provenance": provenance, "network_provenance": network_provenance,
            **trace, **telemetry,
        })
        results_io.write_infra_error(out, {
            "id": task["id"], "outcome": "ARTIFACT_REDACTION_ERROR",
            "error_type": "SecretLeak", "error": json.dumps(secret_hits),
            "at": datetime.now(timezone.utc).isoformat(),
        })
        return ""

    api_error = telemetry["agent_api_error_status"]
    if api_error:
        results_io.write_result(out, {
            "id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": answer,
            "provenance": provenance, "network_provenance": network_provenance,
            **trace, **telemetry,
        })
        results_io.write_infra_error(out, astra_common.rate_limit_rec(task, api_error))
        return ""

    if meta.get("non_mcp_tool_calls"):
        results_io.write_result(out, {
            "id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": answer,
            "provenance": provenance, "network_provenance": network_provenance,
            **trace, **telemetry,
        })
        results_io.write_infra_error(out, {
            "id": task["id"], "outcome": "HARNESS_ERROR", "error_type": "ToolSurfaceViolation",
            "error": f"Codex used non-MCP tools: {meta.get('tool_names')}",
            "at": datetime.now(timezone.utc).isoformat(),
        })
        return ""

    rec = _annotate_incidental(
        _bounded("scoring", _score_openbook, ctrl, task, answer, out, bundle_dir, proxy_tags),
        clean_finish,
    )
    if answer.strip().upper() == "FAIL":
        eval_state = {
            "capture_status": "not_applicable",
            "reason": "agent returned FAIL; official evaluator skipped postconfig",
        }
    else:
        eval_state = _bounded(
            "eval-state capture", _capture_eval_state, ctrl, task, out) if ctrl else None
    results_io.write_result(out, {
        "id": task["id"], "bucket": tasks.bucket_of(task),
        "instruction": task["instruction"], "answer": answer,
        "eval_state": eval_state, "provenance": provenance,
        "network_provenance": network_provenance, **trace, **telemetry,
    })
    results_io.write_eval(out, {"id": task["id"], **rec})
    return answer
