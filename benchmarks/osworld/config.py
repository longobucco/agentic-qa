"""OSWorld config from env. OSW_RELEASE picks the task track -- only "verified" (369 tasks,
xlang-ai/OSWorld) is wired up"""
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"

# Knobs of the Phase 1 harness (legacy arms, verify-replan, open-book, app-scope filter). The new
# infrastructure has none of them: an active value is refused rather than silently ignored, so a
# stale shell can never believe it is running an arm. The neutral value (what the legacy harness
# treated as "off") is accepted, since inherited environments still carry it.
_LEGACY_KNOB_NEUTRAL = {
    "OSW_SELF_VERIFY": "0", "OSW_ENFORCE_SANDBOX": "0", "OSW_RESTRICT_RUN_PYTHON": "0",
    "OSW_INLOOP_VERIFY": "0", "OSW_INLOOP_VERIFY_MAX_TURNS": "40",
    "OSW_INLOOP_VERIFY_SKIP_APPS": "os", "OSW_GROUNDING": "0", "OSW_GROUNDING_VERIFY": "1",
    "OSW_GROUNDING_MIN_SCORE": "0.45", "OSW_ZOOM_BATCH": "0",
    "OSW_OBSERVATION": "screenshot+a11y", "OSW_ACTION_SPACE": "pyautogui", "OSW_MAX_TURNS": "150",
    "OSW_INCLUDE_ALL_APPS": "", "OSW_PINNED_EVALUATORS": "1", "OSW_OPENBOOK_IMAGE": "",
}


def _refuse_legacy_knobs(environ=None):
    environ = os.environ if environ is None else environ
    active = [f"{k}={environ[k]!r}" for k, neutral in _LEGACY_KNOB_NEUTRAL.items()
              if environ.get(k, "").strip() not in ("", neutral)]
    active += [f"{k}={v!r}" for k, v in environ.items() if k.startswith("OSW_VR_") and v.strip()]
    if active:
        raise SystemExit("Phase 1 harness knobs are not supported on the new-infrastructure "
                         f"branches (see tag phase1-daytona-frozen): {', '.join(sorted(active))}")


_refuse_legacy_knobs()

RELEASE = os.environ.get("OSW_RELEASE", "verified").strip().lower()
if RELEASE not in ("verified",):
    raise SystemExit(f"OSW_RELEASE={RELEASE!r}: expected 'verified' (unset also works)")
TASKS_FILE = DATA_DIR / f"osworld_{RELEASE}.jsonl"

# The single most consequential knob in the harness, and until 2026-09-07 the ONLY one that was
# neither pinned nor recorded. With this unset, runners/agent_computer passes no --model, so every
# `claude -p` subprocess silently inherits whatever the CLI's current default happens to be -- and
# that default drifts (it also follows an interactive /model switch). Auditing `agent_model_usage`
# after the fact showed the G3 campaign had in fact run across THREE models: claude-sonnet-4-6
# (884 runs), claude-sonnet-5 (326) and claude-opus-4-8 (285). That silently breaks the
# "fixed model, variable harness" premise every comparison in this project rests on, so from here
# on the model is pinned explicitly, recorded in each run's provenance, and cross-checked against
# what the CLI reports it actually used (see agent_computer._provenance / _model_mismatch).
# OSW_MODEL is the name to use; WV_MODEL stays accepted for continuity with the other benchmarks.
MODEL = (os.environ.get("OSW_MODEL", "").strip()
         or os.environ.get("WV_MODEL", "").strip())

# Results live under <RESULTS_DIR>/<system>/, and `system` is the runner's name (see core.run).
# Deriving the runner name from the pinned model gives each model its own results tree for free,
# so a new pinned campaign can never overwrite the older mixed-model one -- which, having runs
# both with and without a saved transcript, has to be preserved exactly as it is.
_MODEL_SLUGS = {
    "claude-sonnet-5": "sonnet5",
    "claude-sonnet-4-6": "sonnet46",
    "claude-opus-4-8": "opus48",
}


def model_slug(model=None):
    model = MODEL if model is None else model
    if not model:
        return ""
    return _MODEL_SLUGS.get(model, model.replace("claude-", "").replace(".", "").replace("-", ""))


# Protocol knobs (docs/superpowers/plans/2026-09-24-osworld-protocol-alignment.md). Empty/None
# keep the historical behavior (CLI default effort, CLI default output-token limit).
EFFORT = os.environ.get("OSW_EFFORT", "").strip()
_mot = os.environ.get("OSW_MAX_OUTPUT_TOKENS", "").strip()
MAX_OUTPUT_TOKENS = int(_mot) if _mot else None

# Official-fidelity knobs (docs/superpowers/plans/2026-09-24-osworld-official-fidelity.md,
# spec benchmarks/osworld/docs/fidelity-audit.md). The official protocol is the only path this
# branch runs: PROTOCOL is a constant, and OSW_PROTOCOL may only assert what is already true.
PROTOCOL = "official"
_osw_protocol = os.environ.get("OSW_PROTOCOL", "").strip()
if _osw_protocol not in ("", "official"):
    raise SystemExit(f"OSW_PROTOCOL={_osw_protocol!r}: expected '' or 'official'")
BACKEND = os.environ.get("OSW_BACKEND", "").strip() or "daytona"
if BACKEND not in ("daytona", "kvm"):
    raise SystemExit(f"OSW_BACKEND={BACKEND!r}: expected 'daytona' or 'kvm'")
# Upstream timings: lib_run_single.run_single_example (sleep 60 after reset, sleep 20 before
# evaluate) and run_multienv_claude.py --sleep_after_execution 0.5.
SLEEP_AFTER_EXECUTION = float(os.environ.get("OSW_SLEEP_AFTER_EXECUTION", "0.5"))
POST_SETUP_WAIT_S = int(os.environ.get("OSW_POST_SETUP_WAIT_S", "60"))
PRE_EVAL_WAIT_S = int(os.environ.get("OSW_PRE_EVAL_WAIT_S", "20"))
# kvm backend: upstream's Docker provider (desktop_env/providers/docker/provider.py) on a
# user-provided Linux host with /dev/kvm. DOCKER_HOST may be local or ssh://user@host;
# KVM_ADDR is the address the harness uses to reach the container's published ports.
KVM_DOCKER_HOST = os.environ.get("OSW_KVM_DOCKER_HOST", "unix:///var/run/docker.sock").strip()
KVM_ADDR = os.environ.get("OSW_KVM_ADDR", "127.0.0.1").strip()
KVM_QCOW2 = os.environ.get("OSW_KVM_QCOW2", "/opt/osworld/Ubuntu.qcow2").strip()
KVM_QCOW2_SHA256 = os.environ.get("OSW_KVM_QCOW2_SHA256", "").strip()
KVM_IMAGE = os.environ.get("OSW_KVM_IMAGE", "happysixd/osworld-docker").strip()
KVM_CLIENT_PASSWORD = os.environ.get("OSW_KVM_CLIENT_PASSWORD", "password")
# The Claude Code CLI the official protocol was verified on (CLAUDE_BUILTIN_TOOLS, isolation
# flags, transcript layout): the official preflight refuses any other `claude --version`.
CLAUDE_CODE_VERSION = os.environ.get("OSW_CLAUDE_CODE_VERSION", "").strip() or "2.1.280"

# "agent_computer" (unpinned, mixed-model, historical) vs "agent_computer_sonnet5" (pinned).
SYSTEM_NAME = f"agent_computer_{model_slug() or 'unpinned'}"
# An effort override is a different protocol: never pool it into the default-effort tree.
if EFFORT:
    SYSTEM_NAME = f"{SYSTEM_NAME}_effort{re.sub(r'[^a-zA-Z0-9]+', '', EFFORT)}"

# Mirrors ASTRA_SYSTEM_SUFFIX below (same rationale: validating a new guest image digest against
# the frozen population must never silently overwrite the existing campaign's results tree).
SYSTEM_SUFFIX = os.environ.get("OSW_SYSTEM_SUFFIX", "").strip()
if SYSTEM_SUFFIX:
    SYSTEM_NAME = f"{SYSTEM_NAME}_{re.sub(r'[^a-zA-Z0-9]+', '', SYSTEM_SUFFIX) or 'suffix'}"

# Independent GPT Astra replication. It deliberately does not reuse OSW_MODEL: setting the
# Sonnet baseline model must never rename or redirect Astra's result tree.
ASTRA_MODEL = os.environ.get("OSW_ASTRA_MODEL", "gpt-6-astra").strip()
ASTRA_REASONING_EFFORT = os.environ.get("OSW_ASTRA_REASONING_EFFORT", "max").strip()
ASTRA_CODEX_VERSION = os.environ.get("OSW_ASTRA_CODEX_VERSION", "0.153.4").strip()
# Which frozen Astra campaign lock the preflight enforces. Default: the official-fidelity
# 361-task campaign (astra_official361_lock.json) -- the only lock this branch carries.
ASTRA_CAMPAIGN_LOCK = (Path(__file__).resolve().parent
                       / os.environ.get("OSW_ASTRA_CAMPAIGN_LOCK", "astra_official361_lock.json"))


# Opt-in suffix for a deliberately SEPARATE results tree under the same model/effort/codex-
# version identity -- e.g. re-running the frozen population against a new guest image digest
# (config.IMAGE) to validate a fix, without touching or overwriting the existing campaign's
# results/logs (image digest is not one of the knobs astra_system_name() names below, so without
# this it would silently collide with the plain campaign tree). Empty by default: every existing
# caller/result tree is unaffected.
ASTRA_SYSTEM_SUFFIX = os.environ.get("OSW_ASTRA_SYSTEM_SUFFIX", "").strip()


def astra_system_name():
    """Never pool a campaign override into another one's results tree."""
    safe = lambda value: re.sub(r"[^a-zA-Z0-9]+", "", value) or "default"
    base = (f"agent_computer_{safe(ASTRA_MODEL)}_{safe(ASTRA_REASONING_EFFORT)}"
            f"_codex{safe(ASTRA_CODEX_VERSION)}")
    if ASTRA_SYSTEM_SUFFIX:
        safe_suffix = re.sub(r"[^a-zA-Z0-9]+", "", ASTRA_SYSTEM_SUFFIX) or "suffix"
        base = f"{base}_{safe_suffix}"
    # The only protocol this branch runs: always the official one.
    base = f"{base}_official"
    if BACKEND == "kvm":
        base = f"{base}_kvm"
    return base


ASTRA_SYSTEM_NAME = astra_system_name()

# The only results trees the new infrastructure may write or report: <runner>_official[_kvm].
# No Phase 1 tree carries this suffix, so a run here can never write into one.
NEW_INFRA_SYSTEM_RE = re.compile(r"agent_computer_[a-z0-9]+(?:_[a-z0-9]+)*_official(?:_kvm)?")


def assert_new_infra_system(name):
    if not NEW_INFRA_SYSTEM_RE.fullmatch(name or ""):
        raise SystemExit(f"results tree {name!r} is not a new-infrastructure tree "
                         "(agent_computer_…_official[_kvm]); Phase 1 trees are frozen under the "
                         "tag phase1-daytona-frozen")
    return name


# Upstream's step budget (run_multienv_claude.py --max_steps 100) under the official protocol.
MAX_STEPS = int(os.environ.get("OSW_MAX_STEPS", "100"))
# Guest screen size. Must match docker/start.sh's Xvfb line and is passed explicitly to
# SetupController, whose own default (1920x1080) drives {SCREEN_WIDTH}/{SCREEN_WIDTH_HALF}
# substitution in task configs -- the two used to disagree (Xvfb ran 1280x1024).
SCREEN_WIDTH = int(os.environ.get("OSW_SCREEN_WIDTH", "1920"))
SCREEN_HEIGHT = int(os.environ.get("OSW_SCREEN_HEIGHT", "1080"))
TASK_TIMEOUT = int(os.environ.get("OSW_TASK_TIMEOUT") or "14400")

# The only protocol this branch runs: always the official one.
SYSTEM_NAME = f"{SYSTEM_NAME}_official"
if BACKEND == "kvm":
    SYSTEM_NAME = f"{SYSTEM_NAME}_kvm"

IMAGE = os.environ.get(   # pinned by digest -- Daytona caches images by tag, not :latest
    "OSW_IMAGE",
    # 2026-09-24: rebuilt on top of the digest below with the guest screen at 1920x1080 (Xvfb
    # and the VLC prewarm display; the published OSWorld-Verified setting, matching the
    # SetupController's placeholder substitution) and four packages 11 of the 361 published
    # tasks need: evince, eog, totem, picard. Required by env/sandbox.py's screen-size gate:
    # the previous image runs Xvfb at 1280x1024 and every run on it now ends as
    # ENVIRONMENT_ERROR by design.
    "ghcr.io/longobucco/osworld-ab@sha256:"
    "759b54f8b15fbb03544f8ac193ce86115d935a3dd8554a1c44b30f904ed07ea2",
    # --- previous pin, kept for history ---
    # 2026-09-23 (later same day): rebuilt on top of the digest below, adding
    # ENV XDG_CONFIG_HOME=/opt/osw_xdg_unused -- extends the SAME Chrome anti-hijacking fix
    # open-book already had (docker/Dockerfile.osworld-openbook, 2026-09-12) to closed-book.
    # Found live re-validating the CDP-routing fix just below: even with routing fixed, Chrome
    # itself never bound --remote-debugging-port at all ("DevTools remote debugging requires a
    # non-default data directory" in its own log) -- nothing downstream (socat, CdpForwarder)
    # could have worked regardless. Same one-line fix as open-book's; the actual Chrome profile
    # directory used is unaffected (still $HOME/.config/google-chrome).
    #     "ghcr.io/longobucco/osworld-ab@sha256:"
    #     "2c9c677ab54574433f4c41b0446f2c798a2b0acd951bc1de51b2fead5eeac02f"
    # --- previous pin, kept for history ---
    # 2026-09-23: rebuilt on top of the digest below, running VLC as a dedicated non-root user
    # (vlcuser) via a shim at /usr/bin/vlc -- VLC categorically refuses to start as root
    # ("VLC is not supposed to be run as root. Sorry."), confirmed live to be the TRUE root
    # cause of every VLC ENVIRONMENT_ERROR ("launched app(s) never started/rendered: vlc") for
    # all 22 VLC tasks, not the cold-start timing issue previously assumed. Also extends the
    # CDP-routing fix (host-side, env/sandbox.py -- already live without a rebuild) to
    # closed-book: chrome_open_tabs/chrome_close_tabs config steps (51 tasks in the full task
    # set) were unroutable from the harness host, exactly the open-book oracle_unroutable defect
    # fixed there back on 2026-09-13 but never extended to closed-book pending this decision.
    # See docker/vlc-shim.sh and env/sandbox.py's `enable_cdp_forwarder` docstring for the full
    # story on each.
    #     "ghcr.io/longobucco/osworld-ab@sha256:"
    #     "c20e8b979ee31b8b8b760c3b824a7e350f786e662302821712b1460511fe3c29"
    # --- previous pin, kept for history ---
    # 2026-09-22: rebuilt on top of the digest below, adding ONE package: nautilus. Found live
    # re-checking open-book campaign failures -- task 415ef462's own official config has a
    # "launch" step for `nautilus /home/user/Documents/Finance` (GNOME Files), which silently
    # no-op'd because the binary was never installed (same SetupController blind spot as the
    # missing-sudo fix below: only the HTTP status is checked, never the shell command's exit
    # code). 10 tasks in data/osworld_verified.jsonl launch nautilus directly in their own
    # config, so this is generic infra, not a fix aimed at one task's checker. No other change
    # in this rebuild -- the host-side fixes below this pin (env-var-prefix launch parsing,
    # wmctrl -lx window matching, the desktop-readiness gate) all live in env/sandbox.py, which
    # runs on the harness host, not inside the guest image, so they took effect immediately on
    # merge without needing a rebuild.
    #     "ghcr.io/longobucco/osworld-ab@sha256:"
    #     "72ce5805a6568e9f818069aa490d572d560ed80cfe247491892756bf9eb055d3"
    # --- previous pin, kept for history ---
    # 2026-09-16 (later same day): rebuilt again on top of the digest below, carrying the fixes
    # from docs/superpowers/plans/2026-09-16-osworld-closed-book-infra-fixes.md -- targeting the
    # Sonnet 5 closed-book campaign's 16 infra-attributed "0/3"/partial tasks (see that campaign's
    # own forensic report, benchmarks/osworld/docs/sonnet5-open-vs-closed-book.md). Every change
    # was static-verified during implementation (no Docker daemon in that dev environment) and
    # task-reviewed individually plus a whole-branch final review (one Critical finding caught
    # and fixed there, see below) before this digest was built; LIVE validation against a real
    # sandbox is docs/superpowers/plans/...-infra-fixes.md's own Task 11, run separately from this
    # build+push step:
    #   - sudo installed -- SetupController never checks a config/postconfig shell command's own
    #     exit code (only the HTTP status), so a missing sudo silently no-op'd `sudo -S` steps
    #     (tasks e0df059f, 5812b315).
    #   - gnome-settings-daemon installed -- ships the org.gnome.settings-daemon.plugins.power
    #     gsettings schema (compiled separately from gsettings-desktop-schemas), missing before
    #     (task bedcedc4).
    #   - /usr/bin/timedatectl shim (docker/timedatectl-shim.sh) -- is_utc_0 hard-parses
    #     `timedatectl status` line 3; no systemd on this image, so the real binary doesn't exist
    #     (task b6781586).
    #   - HOME=/home/user exported guest-wide in start.sh (not just per-app wrapper hacks) --
    #     apps that resolve their own config dir via $HOME (LibreOffice, VLC) now land in
    #     /home/user/... matching literal vm_file evaluator paths (tasks 2373b66a, 2cd43775,
    #     5ced85fc, 8ba5ae7a).
    #   - GSETTINGS_BACKEND=keyfile exported in start.sh -- gsettings set/get now persist to a
    #     plain file instead of needing a dconf-service session bus this container never had, so
    #     a value set by the agent is visible to a LATER, separate evaluator process (task
    #     3ce045a0).
    #   - openbox Ctrl+Alt+T -> xterm keybinding (docker/openbox-rc.xml) -- openbox's defaults
    #     don't include it (a GNOME/Unity convention), but several evaluator postconfig steps
    #     open a fresh terminal that way (task 13584542).
    #   - VLC plugin-cache pre-warm + vlcrc seed at build time -- three evaluators pkill+relaunch
    #     vlc with zero sleep before reading vlcrc off disk; a cold plugin-cache scan left the
    #     file transiently missing/empty (tasks 9195653c, 215dfd39, a5bbbcd5).
    #   - env.sandbox._verify_launches now also requires a mapped window (wmctrl), not just a
    #     live process, before treating a "launch" config step as started -- a cold-started
    #     process can paint nothing for several seconds, handing the agent a blank first
    #     screenshot. Ships WITH an explicit exemption for socat (a headless CDP-forwarding relay
    #     launched alongside chrome in 79 task configs, confirmed by auditing the full task set)
    #     -- the whole-branch final review caught that the unexempted version would have turned
    #     every one of those 79 tasks into a false ENVIRONMENT_ERROR on every run, regardless of
    #     the agent; fixed and re-reviewed before this digest was built.
    #     (superseded by the digest above -- kept only as a comment, not passed as a default)
    #     "ghcr.io/longobucco/osworld-ab@sha256:"
    #     "dbb3bbf78d05588124e20b6286ebea221a4a97c1a793b11ae2ed40d2418cfe95"
    # --- previous pin, kept for history ---
    # 2026-09-16: rebuilt from a Dockerfile.osworld carrying three fixes, all live-validated
    # against a real sandbox on THIS digest (not just an apt-get-install claim -- see the
    # Dockerfile's own "an apt-get install is not validation" discipline), against the
    # agent_computer_astra closed-book campaign's 32 "0/3" tasks, none of which had reached a
    # pinned digest with these fixes yet (see that campaign's own forensic report):
    #   - VS Code's launch wrapper now uses --user-data-dir=/home/user/.config/Code (a literal
    #     path, NOT $HOME/.config/Code -- this image has no `user` account, everything runs as
    #     root with $HOME=/root, confirmed live via whoami/ps aux, and every vm_file-type VS Code
    #     evaluator in data/osworld_verified.jsonl hands get_vm_file a literal
    #     "/home/user/..." string with zero HOME/~ expansion, confirmed by reading
    #     desktop_env/evaluators/getters/file.py -- unlike google-chrome's getters just above,
    #     which resolve os.getenv('HOME') INSIDE the guest at eval time and so stay consistent
    #     with $HOME on their own). Live-confirmed on a real sandbox: `code` now launches with
    #     --user-data-dir=/home/user/.config/Code, and env.controller.get_file() against
    #     /home/user/.config/Code/User/keybindings.json returns exactly what was written there --
    #     the same round trip check_json_settings/check_json_keybindings perform. Previously a
    #     correct agent write was scored FAILURE every time regardless (4 closed-book tasks:
    #     930fdb3b, 9439a27b, 9d425400, e2b5e914).
    #   - `git` added to the package list -- tasks that shell out to `git clone` (e.g. acb0f96b)
    #     failed on FileNotFoundError regardless of the agent. Live-confirmed: `git --version`
    #     now succeeds on a real sandbox.
    #   - Inherits the D-Bus/AT-SPI bus fix from 4ce715b6 (2026-09-12), which the previous pinned
    #     digest predated -- that commit's own message flagged this exact gap ("the pinned
    #     default digest predates it... a11y_ok in result.json is what confirms or refutes it on
    #     the first real run"). runners/gpt_astra.py now also records a11y_ok/a11y_nodes (it
    #     never did before). Live-confirmed on a real sandbox: an IDLE desktop (nothing launched)
    #     is legitimately a blank black screen with an empty <desktop-frame/> -- that alone is NOT
    #     a bug, and is not what most of the campaign's 16 blank-desktop "0/3" tasks necessarily
    #     were. With an app actually launched (`code`), tree_health went from
    #     {"nodes": 0, "ok": False, "reason": "root node only..."} to
    #     {"nodes": 17, "elements": 12, "ok": True} and the screenshot went from a 2-color (black
    #     + cursor) image to one with 3148 distinct colors -- the AT-SPI bridge itself works once
    #     an app exists to report through it. Whether this actually resolves those 16 tasks (vs. a
    #     separate app-launch failure the bridge fix does not touch) is NOT yet known -- only a
    #     real campaign run against this digest, with a11y_ok now recorded, can show that.
    #     (superseded by the digest above -- kept only as a comment, not passed as a default)
    #     "ghcr.io/longobucco/osworld-ab@sha256:"
    #     "2d3d9665f43b0726eafda32d493bd527ea7437781e09d9c85209063487750640"
)

CONTROLLER_PORT = int(os.environ.get("OSW_CONTROLLER_PORT", "5000"))

CONTROLLER_URL = os.environ.get("OSW_CONTROLLER_URL", "").strip()   # skip provisioning
SANDBOX_ID = os.environ.get("OSW_SANDBOX_ID", "").strip()

# The published OSWorld-Verified population (release minus login tasks) is the only one this
# branch runs: OSW_POPULATION may only assert what is already true.
POPULATION = "verified361"
_osw_population = os.environ.get("OSW_POPULATION", "").strip()
if _osw_population not in ("", "verified361"):
    raise SystemExit(f"OSW_POPULATION={_osw_population!r}: expected '' or 'verified361'")
# "os": a generic desktop/OS-level capability tag (terminal use, file manager, ...) that shows
# up alongside a task's real app tag(s), e.g. ['vlc', 'os'] or ['vscode', 'os'] -- not an
# installable app, same category as "terminal". Found live 2026-08-19: treating it as an
# unsupported app was excluding 85 otherwise-runnable tasks from the population for no reason.
ALWAYS_PRESENT_CAPABILITIES = {"terminal", "os"}   # not apps -- always in the image
