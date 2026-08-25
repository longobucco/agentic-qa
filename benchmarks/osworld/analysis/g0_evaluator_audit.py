"""G0: static audit of the 369 evaluator specs. Offline, no sandbox.

Our classes, not OSWorld's -- OSWorld only has a score in [0,1], pass at >=1.0.
We add two independent axes; only the second is a judgement call.

Axis 1, status (objective facts):
  broken     - func missing from installed desktop_env -> scoring raises (no oracle)
  infeasible - the "infeasible" func: scores refusing an impossible task
  normal     - everything else; only these get a strength

Axis 2, oracle strength (rubric = false-positive rate of the check, cf. Barr et
al., The Oracle Problem in Software Testing, IEEE TSE 2015):
  strong - >=2 fields of a structured artifact, or executable ground truth (sql/pytest)
  medium - one content value (string/number/membership/similarity); brittle to format
  weak   - a state/existence flag, unrelated to the instructed content

Validity: axis 2 is single-coder -- 60 top funcs (>85% of volume) hand-read, the
tail via a heuristic (spot-checked 11/12, only errs toward medium, so strong is a
floor). The definitive check is the empirical brittleness test (gap-research-plan.md),
not yet run; this audit scopes it.

Run: python -m benchmarks.osworld.analysis.g0_evaluator_audit
"""
import collections
import inspect
import json
import re

from benchmarks.osworld import config

# manually read against desktop_env source; see the strength rubric in the docstring
MANUAL_STRONG = {
    "compare_table", "compare_pptx_files", "compare_docx_files", "compare_docx_tables",
    "compare_docx_images", "compare_docx_files_and_ignore_new_lines", "check_mp3_meta",
    "run_sqlite3", "check_python_file_by_test_suite", "check_json", "check_json_keybindings",
    "check_json_settings", "check_thunderbird_prefs", "is_expected_tabs", "is_expected_bookmarks",
    "check_gnome_favorite_apps", "check_moved_jpgs", "check_list", "check_direct_json_object",
    "is_vlc_playing", "check_csv", "compare_csv",
}
MANUAL_MEDIUM = {
    "exact_match", "literal_match", "match_in_list", "is_in_list", "is_in_vm_clickboard",
    "check_include_exclude", "is_expected_url_pattern_match", "file_contains",
    "is_extension_installed", "is_expected_active_tab", "compare_text_file", "diff_text_file",
    "check_config_status", "check_pdf_pages", "compare_images", "check_history_deleted",
    "is_added_to_steam_cart", "compare_audios", "check_structure_sim", "check_structure_sim_resized",
    "check_brightness_decrease_and_structure_sim", "check_contrast_increase_and_structure_sim",
    "check_saturation_increase_and_structure_sim", "check_file_exists_and_structure_sim",
    "check_palette_and_structure_sim", "check_textbox_on_leftside",
}
MANUAL_WEAK = {"is_vlc_fullscreen"}

# parser calls, not bare substrings (a "pptx" substring also hits a var named pptx_file)
STRUCT_CALLS = (r"Document\(", r"Presentation\(", r"PdfReader\(", r"fitz\.open\(",
                r"zipfile\.ZipFile\(", r"sqlite3\.connect\(", r"openpyxl\.load_workbook\(",
                r"pd\.read_csv\(", r"csv\.(reader|DictReader)\(", r"BeautifulSoup\(")

STRENGTHS = ("strong", "medium", "weak")


def _funcs_of(task):
    func = (task.get("evaluator", {}) or {}).get("func")
    return func if isinstance(func, list) else [func] if func else []


def _load_tasks(tasks_path=None):
    with open(tasks_path or config.TASKS_FILE) as f:
        return [json.loads(line) for line in f]


def _heuristic(name, metrics_module):
    """Tail funcs not hand-read: strong only on clear multi-field parsing, else
    medium. Never auto-weak (too easy to misfire on a short strict single-value check)."""
    obj = getattr(metrics_module, name, None)
    if obj is None:
        return "broken"
    try:
        src = inspect.getsource(obj)
    except (OSError, TypeError):
        return "medium"
    has_struct_call = any(re.search(p, src) for p in STRUCT_CALLS)
    n_for = len(re.findall(r"\bfor\s+[^\n]+?\s+in\b", src))  # matches tuple-unpacking loops too
    if has_struct_call and n_for >= 2:
        return "strong"
    return "medium"


def classify_all(tasks_path=None, *, desktop_env_metrics=None):
    """Func-level classes: {func_name: (klass, source)}, klass one of
    strong/medium/weak/infeasible/broken, source 'manual' or 'heuristic'."""
    if desktop_env_metrics is None:
        from desktop_env.evaluators import metrics as desktop_env_metrics

    used = set()
    for t in _load_tasks(tasks_path):
        used.update(_funcs_of(t))

    out = {}
    for name in sorted(used):
        if name == "infeasible":
            out[name] = ("infeasible", "manual")
        elif name in MANUAL_STRONG:
            out[name] = ("strong", "manual")
        elif name in MANUAL_MEDIUM:
            out[name] = ("medium", "manual")
        elif name in MANUAL_WEAK:
            out[name] = ("weak", "manual")
        elif not hasattr(desktop_env_metrics, name):
            out[name] = ("broken", "manual")   # non-resolution is a fact, not a guess
        else:
            out[name] = (_heuristic(name, desktop_env_metrics), "heuristic")
    return out


def _classes_of(task, func_classes):
    return [func_classes.get(f, ("medium", "heuristic"))[0] for f in _funcs_of(task)]


def task_status(task, func_classes):
    """Axis 1: broken if any metric unresolvable, else infeasible if all infeasible,
    else normal ('absent' if no func at all)."""
    classes = _classes_of(task, func_classes)
    if not classes:
        return "absent"
    if "broken" in classes:
        return "broken"
    if all(c == "infeasible" for c in classes):
        return "infeasible"
    return "normal"


def task_strength(task, func_classes):
    """Axis 2, normal tasks only (None otherwise). Weakest metric wins: conj='and'
    needs all, conj='or' guarantees only the weakest."""
    if task_status(task, func_classes) != "normal":
        return None
    order = {"weak": 0, "medium": 1, "strong": 2}
    strengths = [c for c in _classes_of(task, func_classes) if c in order]
    return min(strengths, key=lambda k: order[k]) if strengths else None


def coverage_table(tasks_path=None):
    """Two-axis breakdown over the task set. by_strength counts only 'normal'
    tasks (status != normal has no strength, by construction)."""
    from desktop_env.evaluators import metrics as desktop_env_metrics
    func_classes = classify_all(tasks_path, desktop_env_metrics=desktop_env_metrics)
    tasks = _load_tasks(tasks_path)

    by_status = collections.Counter()
    by_strength = collections.Counter()
    by_app_strength = collections.Counter()
    for t in tasks:
        by_status[task_status(t, func_classes)] += 1
        s = task_strength(t, func_classes)
        if s is not None:
            by_strength[s] += 1
            app = (t.get("related_apps") or ["misc"])[0]
            by_app_strength[(app, s)] += 1

    return {
        "n_tasks": len(tasks),
        "func_classes": func_classes,
        "by_status": dict(by_status),
        "by_strength": dict(by_strength),
        "by_app_strength": {f"{a}/{c}": n for (a, c), n in by_app_strength.items()},
    }


# aliases OSWorld's related_apps uses for the canonical config.SUPPORTED_APPS names
APP_ALIASES = {"calc": "libreoffice_calc", "writer": "libreoffice_writer", "vs_code": "vscode"}


def _norm_app(s):
    s = s.strip().lower().replace(" ", "_").replace("-", "_")
    return APP_ALIASES.get(s, s)


def app_label_audit(tasks_path=None):
    """related_apps is free text: tasks.load_tasks() matches config.SUPPORTED_APPS
    literally, so 'libreoffice calc' (space) or 'vs_code' is dropped from scope even
    though the app is supported. Quantifies the effect."""
    tasks = _load_tasks(tasks_path)

    counts = collections.Counter()
    for t in tasks:
        apps = t.get("related_apps") or []
        if not apps:
            counts["<empty>"] += 1
        for a in apps:
            counts[a] += 1

    supported = config.SUPPORTED_APPS
    supported_norm = {_norm_app(s) for s in supported}
    variants = {label: _norm_app(label) for label in counts
                if label not in supported and _norm_app(label) in supported_norm}

    currently_in = [t for t in tasks if set(t.get("related_apps") or []) <= supported]
    normalized_in = [t for t in tasks
                      if {_norm_app(a) for a in (t.get("related_apps") or [])} <= supported]
    still_unresolved = collections.Counter(
        a for t in tasks if t not in normalized_in for a in (t.get("related_apps") or [])
        if _norm_app(a) not in supported)

    return {
        "label_counts": dict(counts.most_common()),
        "spelling_variants": variants,
        "n_currently_in_scope": len(currently_in),
        "n_in_scope_after_normalization": len(normalized_in),
        "unresolved_tags_blocking_still_excluded_tasks": dict(still_unresolved.most_common()),
    }


# seed(42) sample of 12 tail funcs, hand-read to check the heuristic; truth per-func inline
VALIDATION_SAMPLE = {
    "compare_videos": "strong",              # up to 100 independent frame comparisons (phash)
    "check_image_mirror": "medium",          # single holistic SSIM check
    "check_auto_saving_time": "medium",      # one XML setting value (AutoSaveTimeIntervall)
    "fuzzy_place_math": "strong",            # 3 independently fuzzy-scored answer slots
    "check_qt_max_volume": "medium",         # one config key=value check
    "check_presenter_console_disable": "medium",  # one XML setting value
    "check_page_number_colors": "medium",    # one color-category check
    "check_image_size": "medium",            # one width/height pair
    "is_first_line_centered": "medium",      # one paragraph alignment attribute
    "check_image_file_size": "medium",       # one byte-size threshold
    "evaluate_colored_words_in_tables": "strong",  # delegates compare_docx_files + per-cell check
    "compare_pdf_images": "strong",          # multi-page, multi-image extraction and comparison
}


def validate_heuristic():
    from desktop_env.evaluators import metrics as desktop_env_metrics
    agree, disagree = [], []
    for name, truth in VALIDATION_SAMPLE.items():
        got = _heuristic(name, desktop_env_metrics)
        (agree if got == truth else disagree).append((name, truth, got))
    return {"n": len(VALIDATION_SAMPLE), "agree": len(agree), "disagree": disagree}


def _main():
    cov = coverage_table()
    n = cov["n_tasks"]
    print(f"tasks: {n}")

    print("\naxis 1 - status (objective):")
    for st, c in sorted(cov["by_status"].items(), key=lambda x: -x[1]):
        print(f"  {st:<12}{c:>4}  ({100*c/n:.1f}%)")

    normal = sum(cov["by_strength"].values())
    print(f"\naxis 2 - oracle strength (rubric; over {normal} 'normal' tasks):")
    for s in STRENGTHS:
        c = cov["by_strength"].get(s, 0)
        print(f"  {s:<12}{c:>4}  ({100*c/normal:.1f}% of normal)")

    n_manual = sum(1 for _, src in cov["func_classes"].values() if src == "manual")
    n_heur = sum(1 for _, src in cov["func_classes"].values() if src == "heuristic")
    print(f"\nfunc classification source: {n_manual} manual-read, {n_heur} heuristic-only "
          f"(of {len(cov['func_classes'])} distinct funcs)")

    v = validate_heuristic()
    print(f"heuristic validation: {v['agree']}/{v['n']} agree with manual read")
    for name, truth, got in v["disagree"]:
        print(f"  DISAGREE {name}: heuristic={got} manual={truth}")

    print("\nstrength by app (normal tasks only):")
    for k, c in sorted(cov["by_app_strength"].items()):
        print(f"  {k:<30}{c}")

    audit = app_label_audit()
    print(f"\nscope: {audit['n_currently_in_scope']} tasks with literal SUPPORTED_APPS match, "
          f"{audit['n_in_scope_after_normalization']} with case/spelling normalized -- "
          f"{audit['n_in_scope_after_normalization'] - audit['n_currently_in_scope']} tasks "
          f"excluded by label spelling alone, not by missing app support")
    for label, norm in audit["spelling_variants"].items():
        print(f"  {label!r:<25} -> {norm!r} (count={audit['label_counts'][label]})")


if __name__ == "__main__":
    _main()
