"""Fetch desktop_env's EVALUATORS at the pinned upstream commit -> data/evaluators/.

  python -m benchmarks.osworld.data.download_evaluators

Why this exists: the task set and the guest server image are both pinned to
`download_data.UPSTREAM_COMMIT`, but until 2026-09-08 the code that computes the verdict was
not pinned to anything -- it was whatever `pip install desktop_env` had resolved (1.0.2 on this
machine). Six tasks in the verified release call metrics/getters that release doesn't have
(`compare_references_gain`, `check_play_and_exit`, `compare_pptx_files_tolerant`,
`check_continuation_line_indent_no_bullet`, `get_local_file`, `get_chrome_appearance_mode_ui`,
and `compare_docx_tables(ignore_case_rows=...)`), and every run on them died with an
AttributeError filed as EVAL_ERROR. All seven symbols DO exist at the pinned commit: the
library was behind the task set, which is a pinning gap on our side, not an upstream bug.

Why a download and not `pip install git+...@<commit>`: upstream's pyproject declares
`requires-python >=3.12` (this venv is 3.11) and pulls paddleocr/paddlepaddle and a dozen model
SDKs for the *agent* half of that repo, none of which scoring needs. Fetching the 29 evaluator
files is the same mechanism `download_data.py` already uses for the tasks and
`docker/Dockerfile.osworld` for the guest server -- one commit, three artifacts, one bump.

The installed `desktop_env` stays in place: it still provides the controllers and the
third-party dependencies the evaluators import. Only `desktop_env.evaluators` is overlaid, by
env/osworld_eval.py::use_pinned_evaluators.
"""
import json
import pathlib
import urllib.request

from benchmarks.osworld.data.download_data import UPSTREAM_COMMIT

SUBTREE = "desktop_env/evaluators/"
TREE_API = ("https://api.github.com/repos/xlang-ai/OSWorld/git/trees/"
            f"{UPSTREAM_COMMIT}?recursive=1")
RAW_BASE = f"https://raw.githubusercontent.com/xlang-ai/OSWorld/{UPSTREAM_COMMIT}/"

OUT = pathlib.Path(__file__).resolve().parent / "evaluators"
STAMP = OUT / "PINNED_COMMIT"       # what env/osworld_eval.py checks before trusting the tree


def _fetch(url, *, timeout=60):
    return urllib.request.urlopen(url, timeout=timeout).read()


def main():
    print(f"listing {SUBTREE} at {UPSTREAM_COMMIT[:8]} ...")
    tree = json.loads(_fetch(TREE_API).decode())
    if "tree" not in tree:
        raise SystemExit(f"GitHub API said: {tree.get('message', tree)}")
    blobs = [e["path"] for e in tree["tree"]
             if e["type"] == "blob" and e["path"].startswith(SUBTREE)]
    if not blobs:
        raise SystemExit(f"no files under {SUBTREE} at {UPSTREAM_COMMIT} -- refusing to write "
                         f"a half-empty evaluator tree")

    n = 0
    for path in sorted(blobs):
        dest = OUT / path[len(SUBTREE):]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_fetch(RAW_BASE + path))
        n += 1
    STAMP.write_text(UPSTREAM_COMMIT + "\n")
    print(f"wrote {n} file(s) -> {OUT}")
    print("scoring picks this up automatically; verify with:\n"
          "  python -c \"from benchmarks.osworld.env import osworld_eval as e; "
          "print(e.evaluator_provenance())\"")


if __name__ == "__main__":
    main()
