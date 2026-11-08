"""Score one externally-run OSWorld unit against our own official evaluator.

For a run executed by infrastructure outside this repo (different harness, no result.json/
eval.json of ours) that still used an OSWorld-Verified task and left its guest VM's controller
reachable before teardown: this calls the SAME capture/scoring path as
runners/agent_computer.py's own tail (`_capture_eval_state` + `_score`), against that external
controller_url, and writes result.json/eval.json in our own layout so the run becomes a normal
entry under benchmarks/osworld/results/<tree>/<task_id>/run_<k>/ -- readable by
campaign_analysis and core.reporting with no changes there.

Needs: the task's guest kept ALIVE and its controller (env/controller.py) reachable from here
at the given URL until this call returns (no fixed window -- it's done once this exits).

  python -m benchmarks.osworld.score_external \\
      --tree agent_computer_gpt6astra_ext_pkgfix3_a0_official \\
      --task-id <uuid> --run 1 --controller-url https://<daytona-proxy-host>/ \\
      --answer "DONE" --model gpt-6-astra --source-note "ext-site trial run"
"""
import argparse
import sys

from benchmarks.osworld import config, tasks
from benchmarks.osworld.env.controller import Controller
from benchmarks.osworld.runners.common import _capture_eval_state, _score
from core import results as results_io


def _find_task(task_id):
    for t in tasks.load_tasks():
        if t["id"] == task_id:
            return t
    raise SystemExit(f"task {task_id!r} not found in the loaded task set")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m benchmarks.osworld.score_external")
    p.add_argument("--tree", required=True, help="results subfolder (the 'system' name)")
    p.add_argument("--task-id", required=True)
    p.add_argument("--run", type=int, default=1, dest="run_idx")
    p.add_argument("--controller-url", required=True,
                    help="reachable from here; the guest must still be up")
    p.add_argument("--answer", default="", help="agent's final answer/action, if any")
    p.add_argument("--model", default=None, help="informational only, goes in result.json")
    p.add_argument("--source-note", default=None,
                    help="free text recorded in result.json (which external run this is)")
    a = p.parse_args(argv)

    task = _find_task(a.task_id)
    ctrl = Controller(a.controller_url)
    out = results_io.run_dir(config.RESULTS_DIR, a.tree, a.task_id, a.run_idx)

    eval_state = _capture_eval_state(ctrl, task, out)
    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "instruction": task["instruction"],
        "answer": a.answer,
        "eval_state": eval_state,
        "provenance": {"source": "external", "controller_url": a.controller_url,
                        "model_requested": a.model, "note": a.source_note},
    })
    rec = _score(ctrl, task, a.answer, out)
    results_io.write_eval(out, {"id": task["id"], **rec})
    print(f"{task['id']} run_{a.run_idx} -> {rec['verdict']} ({rec.get('reason', '')})")
    return 0 if rec["verdict"] != "EVAL_ERROR" else 1


if __name__ == "__main__":
    sys.exit(main())
