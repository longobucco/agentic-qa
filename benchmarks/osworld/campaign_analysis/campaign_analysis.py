#!/usr/bin/env python3
"""Unified analysis of every OSWorld campaign (old + new infrastructure, Claude + GPT-Astra).

Reads raw result trees (read-only) and writes: per-run table, per-task cross-arm pages, aggregate
tables (outcomes, failure causes, timing, tokens, cost, coverage, cross-arm comparison).

Usage: campaign_analysis.py <archive_root> <out_dir> [manual_notes.json]
Heuristic cause labels are explicit; `manual_notes.json` carries hand-verified findings per task.
"""
import base64, collections, csv, datetime as dt, hashlib, io, json, os, re, statistics, sys
from pathlib import Path
from PIL import Image

ARCH = Path(sys.argv[1])
OUT = Path(sys.argv[2])
NOTES = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 and Path(sys.argv[3]).exists() else {}
LIMIT = int(os.environ.get("LIMIT", "0"))
VERIF = json.loads(Path(sys.argv[4]).read_text()) if len(sys.argv) > 4 and Path(sys.argv[4]).exists() else {}          # debug: max runs per campaign

A = ARCH / "astra/benchmarks/osworld/results"
B = ARCH / "osworld-vm-base/benchmarks/osworld/results"
C = ARCH / "osworld-closed-book-infra-fixes/benchmarks/osworld/results"

# (tree, root, arm, variant, is_main)   arm = headline group; main = counts in the arm headline numbers
CAMPAIGNS = [
    ("agent_computer", A, "sonnet46-old", "main", True),
    ("agent_computer_sonnet5", A, "sonnet5-old", "main", True),
    ("verify_replan_sonnet5", A, "sonnet5-old", "verify-replan", False),
    ("agent_computer_sonnet5_infrafix", C, "sonnet5-old", "infrafix", False),
    ("agent_computer_astra", A, "astra-old", "main", True),
    ("agent_computer_astra_infracanary20260921", A, "astra-old", "infra-canary", False),
    ("agent_computer_astra_broadcanary20260922", A, "astra-old", "broad-canary", False),
    ("agent_computer_astra_vlccdpvalidation20260923", A, "astra-old", "vlc-cdp-validation", False),
    ("agent_computer_astra_xdgcdpvalidation20260923", A, "astra-old", "xdg-cdp-validation", False),
    ("agent_computer_astra_quotaprobe20260922", A, "astra-old", "quota-probe", False),
    ("agent_computer_astra_probe", A, "astra-old", "probe", False),
    ("agent_computer_astra_openbook", A, "astra-old-openbook", "main", True),
    ("agent_computer_astra_broadcanary20260922_openbook", A, "astra-old-openbook", "broad-canary", False),
    ("agent_computer_astra_openbookfailuresrecheck20260922_openbook", A, "astra-old-openbook", "failures-recheck", False),
    ("agent_computer_astra_openbookcanarynautilus20260922_openbook", A, "astra-old-openbook", "nautilus-canary", False),
    ("agent_computer_sonnet5_effortmax_protocol361img3_official", B, "sonnet5-new", "main", True),
    ("agent_computer_sonnet5_effortmax_cc21282_official", B, "sonnet5-new", "cc21282", False),
    ("agent_computer_sonnet5_effortmax_canary20260924_official", B, "sonnet5-new", "canary", False),
    ("agent_computer_sonnet5_effortmax_protocol361img2_official", B, "sonnet5-new", "img2", False),
    ("agent_computer_sonnet5_effortmax_smoke20260924_official", B, "sonnet5-new", "smoke", False),
    ("agent_computer_gpt6astra_max_codex01534_protocol361img3_official", B, "astra-new", "main", True),
    ("agent_computer_gpt6astra_max_codex01534_protocol361_official", B, "astra-new", "protocol361", False),
    ("agent_computer_gpt6astra_max_codex01534_canary20260924_official", B, "astra-new", "canary", False),
    ("agent_computer_gpt6astra_max_codex01534_protocol361img2_official", B, "astra-new", "img2", False),
    ("agent_computer_gpt6astra_max_codex01534_smoke20260924_official", B, "astra-new", "smoke", False),
]
ARMS = ["sonnet46-old", "sonnet5-old", "astra-old", "astra-old-openbook", "sonnet5-new", "astra-new"]
ARM_DESC = {
    "sonnet46-old": "Claude Sonnet 4.6 (+haiku helper), Phase-1 Daytona harness, 150 turns, 2700 s, early campaign (offline evaluator fallback common)",
    "sonnet5-old": "Claude Sonnet 5, Phase-1 Daytona harness, 150 turns, 2700 s, model pinned",
    "astra-old": "gpt-6-astra via Codex, Phase-1 Daytona harness, closed-book",
    "astra-old-openbook": "gpt-6-astra via Codex, Phase-1 harness, open-book arm (different protocol; reported separately)",
    "sonnet5-new": "Claude Sonnet 5 (effort max), official protocol, Daytona, 1080p, 100 steps",
    "astra-new": "gpt-6-astra (reasoning max) via Codex, official protocol, Daytona, 1080p, 100 steps",
}
NEW_ARMS = {"sonnet5-new", "astra-new"}

BUCKET_MAP = {"calc": "libreoffice_calc", "libreoffice calc": "libreoffice_calc", "vs_code": "vscode", "oS": "os", "?": "unknown"}
BLOCKED = re.compile(r"(block|denied|don.t have access|captcha|robot|bot detect|forbidden|verify you are human|unusual traffic)", re.I)
DESKTOP_DEAD = re.compile(r"(screen (has |is |stays |remain\w* )?(completely )?black|black screen|desktop (remains?|is|was|never|not)\W+(\w+\W+){0,3}(black|render|load|unresponsive|accessible|available)|never (became|become) available|environment (is |was )?(unresponsive|not rendering)|empty accessibility tree|no desktop)", re.I)
MISSING = re.compile(r"(not installed|command not found|isn.t installed|is not available|no module named|does not exist|doesn.t exist|no such file|not found on|no email account|no account (is )?configured|precondition|missing (file|folder|precondition)|unable to find the|could not find the|was unable to find|couldn.t find the)", re.I)
STEPLIMIT = re.compile(r"(step limit|ran out of (the )?(allotted |available )?steps|out of steps|reached the (maximum|max) (number of )?steps|100/100)", re.I)
ERR_RE = re.compile(r"HTTP Error|Error in action|Connection refused|timed out|Traceback", re.I)
RUN_RE = re.compile(r"^run_\d+$")
CLI_ERR = re.compile(r"(hit your (session|usage|weekly|org\w*) limit|usage limit reached|Not logged in|Please run /login|credit balance is too low|Usage credits required|Invalid API key|API Error|overloaded_error|You've reached your)", re.I)


def cli_kind(msg):
    m = (msg or "").strip()
    if not m or len(m) > 260:
        return None
    x = CLI_ERR.search(m)
    if not x:
        return None
    t = x.group(1).lower()
    return "weekly-limit" if "weekly" in t else "session-limit" if ("session" in t or "usage" in t or "reached" in t) else "not-logged-in" if ("logged" in t or "login" in t or "api key" in t) else "credits" if ("credit" in t) else "api-error"


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def pts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def norm_bucket(b):
    b = (b or "unknown").strip()
    b = BUCKET_MAP.get(b, b)
    return b.lower().replace(" ", "_")


def fmt_action(a):
    if not isinstance(a, dict):
        return str(a)[:80]
    act = a.get("action", "?")
    extra = {k: v for k, v in a.items() if k != "action"}
    for k in ("text",):
        if k in extra and len(str(extra[k])) > 60:
            extra[k] = str(extra[k])[:60] + "…"
    return f"{act}({', '.join(f'{k}={v}' for k, v in extra.items())})" if extra else act


def acts_of(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return [args[:100]]
    if not isinstance(args, dict):
        return [str(args)[:100]]
    if isinstance(args.get("actions"), list):
        return [fmt_action(a) for a in args["actions"]]
    return [fmt_action(args)]


def thumb_stats(b64):
    try:
        im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("L").resize((96, 54))
        px = list(im.getdata())
        return round(sum(p < 12 for p in px) / len(px), 3)
    except Exception:
        return None


def png_black(path):
    try:
        im = Image.open(path).convert("L").resize((96, 54))
        px = list(im.getdata())
        return round(sum(p < 12 for p in px) / len(px), 3)
    except Exception:
        return None


# ----------------------------------------------------------------------------- transcripts
def new_conv():
    return dict(steps=[], msgs=[], errors=[], counts=collections.Counter(), max_rep=0, n_calls=0, n_actions=0,
                last_img=None, n_img=0, tools=collections.Counter(), t0=None, t1=None, last="", rep=0)


def push_call(c, acts, txt, bad, err_txt, sigsrc=None):
    c["n_calls"] += 1
    for a in acts:
        c["counts"][a.split("(")[0]] += 1
        c["n_actions"] += 1
    sig = hashlib.md5((sigsrc if sigsrc is not None else "|".join(acts)).encode()).hexdigest()
    c["rep"] = c["rep"] + 1 if sig == c["last"] else 1
    c["last"] = sig
    c["max_rep"] = max(c["max_rep"], c["rep"])
    if bad:
        c["errors"].append((c["n_calls"], "; ".join(acts)[:120], err_txt[:240]))
    c["steps"].append(("call", "; ".join(acts) + (f"   ⟶ {txt}" if txt and txt != "Success" else "")))


def parse_codex(path):
    c = new_conv()
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            t, it = e.get("type"), e.get("item") or {}
            if t == "item.completed" and it.get("type") == "agent_message":
                c["msgs"].append(it.get("text", ""))
                c["steps"].append(("msg", it.get("text", "")))
            elif t == "item.completed" and it.get("type") == "mcp_tool_call":
                res = it.get("result") if isinstance(it.get("result"), dict) else {}
                for b in res.get("content") or []:
                    if b.get("type") == "image" and b.get("data"):
                        c["n_img"] += 1
                        c["last_img"] = b["data"]
                txt = " ".join(b.get("text", "") for b in (res.get("content") or []) if b.get("type") == "text")[:200]
                bad = it.get("error") or it.get("status") not in ("completed", None) or bool(txt and ERR_RE.search(txt))
                tool = it.get("tool") or "mcp"
                c["tools"]["mcp__osworld__" + tool] += 1
                args = it.get("arguments")
                if isinstance(args, dict) and ("action" in args or "actions" in args):
                    acts = acts_of(args)
                else:
                    arg = json.dumps(args, ensure_ascii=False)[:90] if args else ""
                    acts = [f"{tool}({arg})" if arg and arg != "{}" else tool]
                push_call(c, acts, txt, bad, str(it.get("error") or txt), sigsrc=tool + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
            elif t == "item.completed" and it.get("type") not in ("agent_message", "mcp_tool_call", None):
                c["tools"]["other:" + str(it.get("type"))] += 1
                c["steps"].append(("other", json.dumps(it)[:200]))
            elif t in ("error", "turn.failed"):
                c["errors"].append((c["n_calls"], t, json.dumps(e)[:300]))
    return c


def parse_claude(path):
    c = new_conv()
    pending = {}
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = pts(e.get("timestamp") or "") if e.get("timestamp") else None
            if ts:
                c["t0"] = c["t0"] or ts
                c["t1"] = ts
            if e.get("type") not in ("assistant", "user"):
                continue
            m = e.get("message") or {}
            content = m.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for b in blocks:
                bt = b.get("type")
                if e["type"] == "assistant" and bt == "text" and b.get("text"):
                    c["msgs"].append(b["text"])
                    c["steps"].append(("msg", b["text"]))
                elif e["type"] == "assistant" and bt == "tool_use":
                    name = b.get("name", "")
                    c["tools"][name] += 1
                    inp = b.get("input")
                    if name == "mcp__osworld__computer":
                        acts = acts_of(inp)
                    else:
                        short = name.replace("mcp__osworld__", "")
                        arg = json.dumps(inp, ensure_ascii=False)[:90] if inp else ""
                        acts = [f"{short}({arg})" if arg and arg != "{}" else short]
                    pending[b.get("id")] = (name, acts, json.dumps(inp, sort_keys=True, ensure_ascii=False))
                elif e["type"] == "user" and bt == "tool_result":
                    name, acts, full = pending.pop(b.get("tool_use_id"), ("?", ["?"], ""))
                    cc = b.get("content")
                    items = cc if isinstance(cc, list) else [{"type": "text", "text": str(cc)}]
                    txt = ""
                    for x in items:
                        if x.get("type") == "image":
                            src = x.get("source") or {}
                            if src.get("data"):
                                c["n_img"] += 1
                                c["last_img"] = src["data"]
                        elif x.get("type") == "text":
                            txt += x.get("text", "") + " "
                    txt = txt.strip()[:200]
                    bad = bool(b.get("is_error")) or bool(ERR_RE.search(txt))
                    if name.startswith("mcp__osworld__"):
                        push_call(c, acts, txt, bad, txt, sigsrc=name + full)
                    else:
                        c["steps"].append(("other", f"{name}: {'; '.join(acts)[:120]} ⟶ {txt[:80]}"))
                        if bad:
                            c["errors"].append((c["n_calls"], name, txt))
    return c


# ----------------------------------------------------------------------------- artifact diff (xlsx/pptx/text/png)
def _xlsx_diff(a, b, limit=10):
    import openpyxl
    out = []
    try:
        wa, wb = openpyxl.load_workbook(a), openpyxl.load_workbook(b)
    except Exception as e:
        return [f"cannot open xlsx: {e}"]
    if wa.sheetnames != wb.sheetnames:
        out.append(f"sheet names differ: agent={wa.sheetnames} gold={wb.sheetnames}")
    n = ncase = 0
    for name in wa.sheetnames:
        if name not in wb.sheetnames:
            continue
        sa, sb = wa[name], wb[name]
        for r in range(1, max(sa.max_row, sb.max_row) + 1):
            for c in range(1, max(sa.max_column, sb.max_column) + 1):
                va, vb = sa.cell(r, c).value, sb.cell(r, c).value
                if va != vb and not (isinstance(va, (int, float)) and isinstance(vb, (int, float)) and abs(va - vb) < 1e-9):
                    n += 1
                    if isinstance(va, str) and isinstance(vb, str) and va.lower() == vb.lower():
                        ncase += 1
                    if n <= limit:
                        out.append(f"[{name}] {sa.cell(r, c).coordinate}: agent={str(va)[:50]!r} gold={str(vb)[:50]!r}")
        ha = sorted(k for k, d in sa.column_dimensions.items() if d.hidden); hb = sorted(k for k, d in sb.column_dimensions.items() if d.hidden)
        ra = sorted(k for k, d in sa.row_dimensions.items() if d.hidden); rb = sorted(k for k, d in sb.row_dimensions.items() if d.hidden)
        if ha != hb or ra != rb:
            out.append(f"[{name}] hidden cols agent={ha} gold={hb}; hidden rows agent={ra[:8]} gold={rb[:8]}")
        if len(sa._charts) != len(sb._charts):
            out.append(f"[{name}] charts agent={len(sa._charts)} gold={len(sb._charts)}")
    out.insert(0, f"{n} differing cell(s)" + (f" ({ncase} case-only)" if ncase else ""))
    if n and ncase == n:
        out.insert(0, "ALL-DIFFS-CASE-ONLY")
    return out


def _pptx_diff(a, b, limit=8):
    from pptx import Presentation
    try:
        pa, pb = Presentation(a), Presentation(b)
    except Exception as e:
        return [f"cannot open pptx: {e}"]
    out = [f"slides agent={len(pa.slides)} gold={len(pb.slides)}"]
    n = 0
    for i, (sa, sb) in enumerate(zip(pa.slides, pb.slides), 1):
        ta = [(sh.shape_type, sh.has_text_frame and sh.text_frame.text) for sh in sa.shapes]
        tb = [(sh.shape_type, sh.has_text_frame and sh.text_frame.text) for sh in sb.shapes]
        if ta != tb:
            n += 1
            if n <= limit:
                out.append(f"slide {i}: agent={[x[1] for x in ta][:3]} gold={[x[1] for x in tb][:3]}")
        try:
            if sa.background.fill.type != sb.background.fill.type:
                out.append(f"slide {i}: background fill type agent={sa.background.fill.type} gold={sb.background.fill.type}")
        except Exception:
            pass
    return out


def artifact_diff(rdir, ev):
    arts = (ev or {}).get("eval_artifacts") or []
    gold = [a for a in arts if a.get("is_gold") or "gold" in a["name"].lower()]
    agent = [a for a in arts if a not in gold and a.get("kept")]
    if not gold or not agent:
        return None
    import difflib
    lines = []
    for ag in agent[:2]:
        pa = Path(rdir) / "eval_artifacts" / ag["name"]
        gd = max(gold, key=lambda g: len(os.path.commonprefix([g["name"].lower(), ag["name"].lower()])))
        pg = Path(rdir) / "eval_artifacts" / gd["name"]
        if not pa.exists():
            pa = Path(rdir) / ag["name"]
        if not pa.exists() or not pg.exists():
            continue
        lines.append(f"{ag['name']} ({ag.get('size')} B) vs gold {gd['name']} ({gd.get('size')} B); sha equal={ag.get('sha256') == gd.get('sha256')}")
        ext = pa.suffix.lower()
        try:
            if ext in (".xlsx", ".xlsm"):
                lines += _xlsx_diff(pa, pg)
            elif ext == ".pptx":
                lines += _pptx_diff(pa, pg)
            elif ext in (".txt", ".csv", ".json", ".py", ".log", ".md", ".html"):
                d = list(difflib.unified_diff(pg.read_text(errors="replace").splitlines(), pa.read_text(errors="replace").splitlines(), "gold", "agent", n=0, lineterm=""))
                lines += [f"{len(d)} diff line(s):"] + [x[:140] for x in d[:16]]
            elif ext in (".png", ".jpg", ".jpeg"):
                ia, ib = Image.open(pa), Image.open(pg)
                lines.append(f"image sizes agent={ia.size} gold={ib.size}")
        except Exception as e:
            lines.append(f"diff failed: {type(e).__name__}: {e}")
    return lines or None


def checker_of(reason):
    m = re.match(r"official (.*?) -> ", reason or "")
    return m.group(1) if m else None


# ----------------------------------------------------------------------------- classification
def classify(r):
    """-> (outcome, cause, explanation, mode, factor).
    mode   = what the agent/harness did (proximate failure mode)
    factor = environment condition that contributed (kept separate: never blamed alone, see METHODOLOGY.md)."""
    v, reason = r["verdict"], r["reason"] or ""
    steps = r["steps"] or 0
    final = r["final_msg"] or ""
    declared_fail = (r["answer"] or "").strip().upper() == "FAIL" or "[INFEASIBLE]" in final.upper()
    black = r.get("black_last")
    if v is None:
        et = r["infra_last_type"] or r["stop_reason"] or "unknown"
        return "INCOMPLETE", f"no-verdict:{et}", f"No eval.json; last infra error '{et}'; stop_reason={r['stop_reason']}.", "no-verdict", et
    ck = cli_kind(final)
    if ck and r.get("transcript_lost") and v in ("SUCCESS", "FAILURE"):
        if v == "SUCCESS":
            return "PASS", "pass:transcript-lost", "Genuine pass: eval.json/final.png predate the transcript, which was later overwritten by an aborted retry ('" + final.strip()[:50] + "'); the agent actions are not recoverable.", "pass", "transcript-lost"
        return "FAIL", "fail:transcript-lost", "Genuine FAILURE verdict (eval.json predates the transcript, later overwritten by an aborted retry '" + final.strip()[:50] + "'); the cause cannot be recovered from the transcript, only from eval reason/final screenshot.", "transcript-lost", "transcript-lost"
    if ck and (steps == 0 or v != "SUCCESS"):
        if v == "SUCCESS":
            return "PASS-NOOP", "pass:no-op-agent-never-ran", f"The CLI returned '{final.strip()[:60]}' (0 output tokens, agent never ran) yet the checker returned 1.0: it accepts the initial state.", "pass-no-op", ck
        if v in ("FAILURE", "EVAL_ERROR", "ENVIRONMENT_ERROR"):
            return "INFRA-CLI", f"infra:cli-{ck}", f"The agent CLI aborted with '{final.strip()[:70]}' ({steps} steps); recorded as {v} but the agent did not (finish) run. Not an agent failure.", "cli-error", ck
    if v == "SUCCESS":
        inf = "infeasible" in reason
        if steps == 0 and not inf:
            return "PASS-NOOP", "pass:no-op", "0 OSWorld tool calls yet the checker returned 1.0: the checker accepts the initial state (or the task needs no action).", "pass-no-op", None
        return "PASS", ("pass:infeasible-declared" if inf else "pass"), ("Infeasible task; agent correctly declared failure." if inf else f"Official checker {r['checker']} returned 1.0."), "pass", None
    if v == "FAILURE" and (r["api_status"] == 429 or str(r["stop_reason"]) in ("error", "api_error")) and not r["tool_violation"]:
        return "INFRA-API", f"infra:api-error-{r['api_status'] or r['stop_reason']}", f"Agent run aborted by an API error (status {r['api_status']}, stop {r['stop_reason']}) after {steps} steps; recorded as FAILURE but not an agent verdict.", "api-error", str(r["api_status"] or r["stop_reason"])
    if v == "EVAL_ERROR":
        return "ORACLE-ERROR", "eval-error:" + (r["source"] or "?"), f"Evaluator unavailable/errored ({r['source']}): {reason[:140]}", "eval-error", None
    if v == "ENVIRONMENT_ERROR":
        return "INFRA-ERROR", "environment-error", f"Environment error: {reason[:160]}", "environment-error", None
    # ---- FAILURE: proximate mode + environment factor
    factor = None
    if r["n_http_err"] >= 3 or (r["n_http_err"] >= 2 and steps <= 12):
        factor = "tool-http-error"
    elif declared_fail and BLOCKED.search(final):
        factor = "site-blocked"
    elif DESKTOP_DEAD.search(final) or (declared_fail and black is not None and black >= 0.85):
        factor = "black-desktop"
    elif declared_fail and MISSING.search(final):
        factor = "missing-precondition"
    step_limit_msg = bool(STEPLIMIT.search(final))
    if r["tool_violation"]:
        mode, why = "tool-surface-violation", f"Harness rule violation ({', '.join(r['tool_violation'])}); scored FAILURE regardless of end state."
    elif r["contaminated"]:
        mode, why = "contaminated", "Run flagged contaminated: " + "; ".join(json.dumps(x) if not isinstance(x, str) else x for x in r["contam_evidence"])[:160]
    elif r["artifact_diff"] and "ALL-DIFFS-CASE-ONLY" in r["artifact_diff"]:
        mode, why = "checker-strict-case-only", "Output equals the gold except letter case in formulas/strings; rejected by the strict comparison."
    elif r["cap_hit"] and steps == 0 and not final.strip():
        mode, why = "capped-without-acting", f"Run ended on {r['stop_reason']} with 0 OSWorld tool calls (agent never acted on the desktop)."
    elif r["cap_hit"] and (str(r["stop_reason"]).lower().find("max_turn") >= 0 or "timeout" in str(r["stop_reason"]).lower() or step_limit_msg or not (r["answer"] or "").strip()):
        mode, why = "capped", f"Run ended on a cap ({r['stop_reason']}, steps={steps}/{r['cap']}) before satisfying the checker."
    elif steps == 0 and final.strip() and (r["answer"] or "").strip().upper() == "FAIL":
        mode, why = "declined-without-acting", "0 OSWorld tool calls and answer FAIL: the agent refused / asked for confirmation instead of acting."
    elif steps == 0 and final.strip():
        mode, why = "answered-in-text-no-action", "0 OSWorld tool calls: plain-text answer to a task graded on end state."
    elif declared_fail:
        mode, why = "declared-fail", "Agent declared the task infeasible (answer FAIL / [INFEASIBLE]) but the checker expected it done."
    else:
        mode, why = "claimed-done-wrong", f"Agent finished ({steps} steps, answer={r['answer']!r}) but checker {r['checker']} -> 0: end state differs from expected."
    if factor and mode not in ("declared-fail", "capped", "capped-without-acting") and not (factor == "tool-http-error" and steps <= 12):
        factor = None       # a factor is only attributed when the agent could not proceed; runs that finished (claimed done) or refused keep factor=None
    notes = {"tool-http-error": f" The computer tool returned HTTP/connection errors ({r['n_http_err']} calls).",
             "site-blocked": " The live site refused the agent (bot/IP block).",
             "black-desktop": " The agent/last screenshot report a black / unrendered desktop.",
             "missing-precondition": " The agent reports a missing file/app/account the task assumes."}
    if factor:
        why += notes[factor]
    cause = f"fail:{mode}" + (f"+{factor}" if factor else "")
    return "FAIL", cause, why, mode, factor


# ----------------------------------------------------------------------------- per run
def analyze_run(camp, tid, rdir):
    tree, root, arm, variant, main = camp
    res = jload(rdir / "result.json", {}) or {}
    ev = jload(rdir / "eval.json")
    ie = jload(rdir / "infra_error.json", []) or []
    if not isinstance(ie, list):
        ie = [ie]
    pv = res.get("provenance", {}) or {}
    a, b = pts(pv.get("started_at") or ""), pts(pv.get("finished_at") or "")
    conv_path = rdir / "conversation.jsonl"
    conv = None
    if conv_path.exists():
        conv = parse_codex(conv_path) if arm.startswith("astra") else parse_claude(conv_path)
    msgs = conv["msgs"] if conv else []
    ie_types = collections.Counter((h.get("outcome") or h.get("error_type")) if isinstance(h, dict) else str(h) for h in ie)
    ao = rdir / "agent_output.txt"
    final = msgs[-1] if msgs else (ao.read_text(errors="replace")[:700] if ao.exists() else "")
    cost = res.get("agent_cost_usd")
    if cost is None:
        cost = res.get("agent_estimated_api_cost_usd")
    usage = res.get("agent_model_usage") or {}
    cap_steps = pv.get("max_steps")
    cap_turns = pv.get("max_turns")
    stop = res.get("agent_terminal_reason") or res.get("agent_subtype") or res.get("agent_stop_reason")
    steps = res.get("agent_steps_used")
    if steps is None:
        steps = conv["n_calls"] if conv else None
    wall = round((b - a).total_seconds()) if a and b else None
    agent_s = round(res["agent_duration_ms"] / 1000) if res.get("agent_duration_ms") else (round((conv["t1"] - conv["t0"]).total_seconds()) if conv and conv["t0"] and conv["t1"] else None)
    r = dict(
        arm=arm, campaign=tree, variant=variant, main=main, task_id=tid, run=rdir.name,
        bucket=norm_bucket(res.get("bucket")), instruction=res.get("instruction"),
        verdict=(ev or {}).get("verdict"), reward=(ev or {}).get("reward"), reason=(ev or {}).get("reason"),
        source=(ev or {}).get("source"), checker=checker_of((ev or {}).get("reason")),
        tool_violation=(ev or {}).get("tool_surface_violation") or [],
        answer=res.get("answer"), contaminated=bool(res.get("contaminated")), contam_evidence=res.get("contamination_evidence") or [],
        steps=steps, cap=cap_steps or cap_turns, stop_reason=stop, is_error=res.get("agent_is_error"),
        api_status=res.get("agent_api_error_status"), num_turns=res.get("agent_num_turns"),
        clean_finish=res.get("agent_clean_finish"),
        in_tok=res.get("agent_input_tokens"), out_tok=res.get("agent_output_tokens"),
        cache_read_tok=res.get("agent_cache_read_tokens") if res.get("agent_cache_read_tokens") is not None else res.get("agent_cached_input_tokens"),
        cache_write_tok=res.get("agent_cache_creation_tokens"), reasoning_tok=res.get("agent_reasoning_output_tokens"),
        cost_usd=cost, cost_kind="actual" if res.get("agent_cost_usd") is not None else ("estimated" if cost is not None else None),
        wall_s=wall, agent_s=agent_s, started=pv.get("started_at"), finished=pv.get("finished_at"),
        model=pv.get("model_served") or res.get("model_served") or (max(usage.items(), key=lambda kv: (kv[1] or {}).get("outputTokens", 0))[0] if usage else None) or pv.get("model_requested"),
        models_used=",".join(sorted(usage.keys())) if usage else None,
        total_in_tok=(((res.get("agent_input_tokens") or 0) + (res.get("agent_cache_read_tokens") or 0) + (res.get("agent_cache_creation_tokens") or 0)) if res.get("agent_cost_usd") is not None else res.get("agent_input_tokens")),
        effort=pv.get("reasoning_effort_served") or pv.get("effort"), image=(pv.get("image") or "")[-14:],
        infra_attempts=len(ie), infra_types=dict(ie_types),
        infra_last_type=(ie[-1].get("outcome") or ie[-1].get("error_type")) if ie and isinstance(ie[-1], dict) else None,
        n_msgs=len(msgs), final_msg=final,
        has_conv=conv is not None, n_calls=conv["n_calls"] if conv else None, n_actions=conv["n_actions"] if conv else None,
        n_errors=len(conv["errors"]) if conv else 0, max_repeat=conv["max_rep"] if conv else 0,
        n_http_err=sum(1 for _, a, e in conv["errors"] if ERR_RE.search(e) and not a.startswith("run_python")) if conv else 0,
        non_computer_tools=sum(n for k, n in conv["tools"].items() if not k.startswith("mcp__osworld__") and k not in ("computer",) and not k.startswith("other:")) if conv else None,
        tools_used=dict(conv["tools"]) if conv else {},
        action_counts=dict(conv["counts"]) if conv else {},
        path=str(rdir.relative_to(ARCH)),
    )
    def _mt(f):
        q = rdir / f
        return q.stat().st_mtime if q.exists() else None
    em, cm = _mt("eval.json"), _mt("conversation.jsonl")
    r["transcript_vs_eval_min"] = round((cm - em) / 60) if em and cm else None
    r["transcript_after_eval"] = bool(em and cm and cm - em > 600)          # transcript written >10 min after the verdict
    r["transcript_lost"] = bool(r["transcript_after_eval"] and cli_kind(final))  # transcript overwritten by a later aborted retry
    cap_hit = False
    st = str(stop or "").lower()
    if "max_turn" in st or "timeout" in st or "max_steps" in st:
        cap_hit = True
    if cap_steps and steps and steps >= cap_steps:
        cap_hit = True      # refined in classify(): only counts if the run did not finish with an explicit answer, or says so itself
    r["cap_hit"] = cap_hit
    # last screen
    black = None
    fp = rdir / "final.png"
    if conv and conv["last_img"]:
        black = thumb_stats(conv["last_img"])
    elif fp.exists():
        black = png_black(fp)
    r["black_last"] = black
    r["has_final_png"] = fp.exists()
    r["artifact_diff"] = artifact_diff(rdir, ev) if (ev or {}).get("verdict") == "FAILURE" else None
    r["outcome"], r["cause"], r["explanation"], r["mode"], r["factor"] = classify(r)
    r["_conv"] = conv
    return r


def md_run(r, maxl=120):
    L = [f"#### {r['arm']} · {r['campaign'].replace('agent_computer_', '')} · {r['run']} — **{r['outcome']}** · `{r['cause']}`",
         f"- verdict `{r['verdict']}` reward={r['reward']} · {r['reason']}",
         f"- why: {r['explanation']}",
         f"- steps {r['steps']} (cap {r['cap']}) · calls {r['n_calls']} · wall {r['wall_s']}s · agent {r['agent_s']}s · turns {r['num_turns']} · stop `{r['stop_reason']}` · answer `{r['answer']}`",
         f"- tokens in/out/cache-read/cache-write/reason: {r['in_tok']}/{r['out_tok']}/{r['cache_read_tok']}/{r['cache_write_tok']}/{r['reasoning_tok']} · cost ${r['cost_usd']} ({r['cost_kind']}) · model {r['model']}",
         f"- infra attempts: {r['infra_attempts']} {r['infra_types'] or ''} · contaminated={r['contaminated']} · non-computer tool calls={r['non_computer_tools']} · transcript errors={r['n_errors']}",
         f"- actions {r['action_counts']} · max identical repeat {r['max_repeat']} · last-screen black {r['black_last']}",
         f"- raw: `{r['path']}`" + (f" · final screenshot: `{r['path']}/final.png`" if r["has_final_png"] else "")]
    if r["artifact_diff"]:
        L.append("- **output vs gold:**\n  - " + "\n  - ".join(str(x) for x in r["artifact_diff"][:20]))
    if r["final_msg"]:
        L.append("- **final agent message:**\n\n  > " + r["final_msg"].strip().replace("\n", "\n  > ")[:800])
    conv = r["_conv"]
    if conv and conv["errors"]:
        L.append("- **transcript errors (first 6):**")
        for n, act, err in conv["errors"][:6]:
            L.append(f"  - call #{n} `{act}` → {err[:200]}")
    if conv:
        st = conv["steps"]
        view = st if len(st) <= maxl else st[:maxl // 2] + [("gap", f"… {len(st) - maxl} steps omitted …")] + st[-maxl // 2:]
        L.append("\n<details><summary>Transcript digest</summary>\n\n```")
        for k, tx in view:
            L.append(("AGENT: " if k == "msg" else "") + tx[:260].replace("\n", " "))
        L.append("```\n</details>\n")
    return "\n".join(L)


def pct(n, d):
    return f"{100*n/d:.1f}%" if d else "—"


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs)) if xs else None


def p90(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[int(0.9 * (len(xs) - 1))]) if xs else None


def fmtn(x, nd=0):
    return "—" if x is None else (f"{x:,.{nd}f}" if nd else f"{x:,}")


# ----------------------------------------------------------------------------- main
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    runs = []
    for camp in CAMPAIGNS:
        tree, root, arm, variant, main_ = camp
        base = root / tree
        if not base.is_dir():
            print("missing", base)
            continue
        n = 0
        for tdir in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")):
            for rdir in sorted(p for p in tdir.iterdir() if p.is_dir() and RUN_RE.match(p.name)):
                runs.append(analyze_run(camp, tdir.name, rdir))
                n += 1
                if LIMIT and n >= LIMIT:
                    break
            if LIMIT and n >= LIMIT:
                break
        print(f"{arm:20} {variant:18} {tree[:60]:60} {n} runs", flush=True)

    # ---- machine-readable
    cols = [k for k in runs[0] if not k.startswith("_") and k not in ("action_counts", "infra_types", "contam_evidence", "tool_violation", "artifact_diff", "final_msg", "instruction", "explanation")]
    (OUT / "tables").mkdir(exist_ok=True)
    with open(OUT / "tables/runs_all.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols + ["outcome", "cause", "explanation", "final_msg_short"])
        for r in runs:
            w.writerow([r[c] for c in cols] + [r["outcome"], r["cause"], r["explanation"], (r["final_msg"] or "")[:200].replace("\n", " ")])
    with open(OUT / "tables/runs_all.jsonl", "w") as f:
        for r in runs:
            f.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, default=str) + "\n")

    main_runs = [r for r in runs if r["main"]]
    by_arm = {a: [r for r in main_runs if r["arm"] == a] for a in ARMS}
    rec_valid = lambda rs: [r for r in rs if r["verdict"] in ("SUCCESS", "FAILURE")]
    valid = lambda rs: [r for r in rs if r["verdict"] in ("SUCCESS", "FAILURE") and r["outcome"] not in ("INFRA-CLI", "INFRA-API", "PASS-NOOP")]   # informative runs

    # ---- task-level matrix (main campaigns)
    tasks = {}
    for r in runs:
        t = tasks.setdefault(r["task_id"], dict(task_id=r["task_id"], bucket=r["bucket"], instruction=r["instruction"]))
        if not t["instruction"] and r["instruction"]:
            t["instruction"] = r["instruction"]
        if t["bucket"] in ("unknown", "misc") and r["bucket"] not in ("unknown", "misc"):
            t["bucket"] = r["bucket"]
    cell = collections.defaultdict(lambda: [0, 0])      # (task, arm) -> [valid, pass]
    for r in main_runs:
        if r["verdict"] in ("SUCCESS", "FAILURE") and r["outcome"] not in ("INFRA-CLI", "INFRA-API", "PASS-NOOP"):
            c = cell[(r["task_id"], r["arm"])]
            c[0] += 1
            c[1] += r["verdict"] == "SUCCESS"
    with open(OUT / "tables/task_matrix.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task_id", "bucket"] + [f"{a}_pass/valid" for a in ARMS] + ["solved_by_any_arm", "instruction"])
        for tid, t in sorted(tasks.items(), key=lambda kv: (kv[1]["bucket"], kv[0])):
            row = [tid, t["bucket"]]
            anyp = False
            for a in ARMS:
                v, p = cell.get((tid, a), [0, 0])
                row.append(f"{p}/{v}" if v else "")
                anyp = anyp or p > 0
            w.writerow(row + [anyp, t["instruction"]])

    def task_score(a, tid):
        v, p = cell.get((tid, a), [0, 0])
        return p / v if v else None

    # ---- SUMMARY.md
    S = ["# OSWorld campaigns — cross-arm summary\n",
         f"Generated {dt.datetime.now().date()} from the archived result trees. Headline numbers use the **main** campaign of each arm; "
         "every other campaign (canaries, probes, variants) is listed in the campaign table and kept in `tasks/` pages.\n",
         "## Arms\n", "| arm | description |", "|---|---|"]
    for a in ARMS:
        S.append(f"| {a} | {ARM_DESC[a]} |")
    S += ["\n## Headline (main campaigns)\n",
          "**Recorded** = what the harness wrote (`eval.json` verdict SUCCESS/FAILURE). **Informative** = recorded runs minus (a) runs where the agent CLI aborted (session limit, not logged in, API error) but the harness still wrote a verdict, and (b) *no-op passes*: SUCCESS with 0 tool calls on a task that is not infeasible (the checker accepts the initial state). All later tables use informative runs.\n",
          "| arm | tasks tried | tasks w/ informative verdict | run dirs | recorded valid | recorded pass% | CLI/API-aborted (not agent) | no-op passes | transcript lost (verdict valid) | informative runs | informative pass | informative fail | **informative pass%** | oracle err | infra err | no verdict |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in ARMS:
        rs = by_arm[a]
        rv, v = rec_valid(rs), valid(rs)
        tt = {r["task_id"] for r in rs}
        tv = {r["task_id"] for r in v}
        c = collections.Counter(r["verdict"] for r in rs)
        oc = collections.Counter(r["outcome"] for r in rs)
        pv = sum(r["verdict"] == "SUCCESS" for r in v)
        S.append(f"| {a} | {len(tt)} | {len(tv)} | {len(rs)} | {len(rv)} | {pct(c['SUCCESS'], len(rv))} | {oc['INFRA-CLI'] + oc['INFRA-API']} | {oc['PASS-NOOP']} | {sum(1 for r in rs if r['transcript_lost'])} | {len(v)} | {pv} | {len(v) - pv} | **{pct(pv, len(v))}** | {c['EVAL_ERROR']} | {c['ENVIRONMENT_ERROR']} | {c[None]} |")

    # tasks passing with no action
    noop_tasks = collections.defaultdict(lambda: collections.Counter())
    for r in main_runs:
        if r["outcome"] == "PASS-NOOP":
            noop_tasks[r["task_id"]][r["arm"]] += 1
    S += [f"\n### Tasks the checker passes without any agent action ({len(noop_tasks)})\n",
          "Runs with 0 OSWorld tool calls scored SUCCESS (not infeasible tasks). Indicates a checker that accepts the initial state, or a task whose end state is already satisfied. These runs are excluded from the informative numbers.\n",
          "| task | bucket | no-op passes per arm | instruction |", "|---|---|---|---|"]
    for tid, cnt in sorted(noop_tasks.items(), key=lambda kv: (tasks[kv[0]]["bucket"], kv[0])):
        S.append(f"| {tid[:8]} | {tasks[tid]['bucket']} | {dict(cnt)} | {(tasks[tid]['instruction'] or '')[:80]} |")

    # aborted runs by kind
    S += ["\n### Runs recorded as results but aborted by the agent CLI / API (excluded)\n", "| arm | kind | recorded verdict | runs |", "|---|---|---|---|"]
    ab = collections.Counter((r["arm"], r["cause"], r["verdict"]) for r in main_runs if r["outcome"] in ("INFRA-CLI", "INFRA-API"))
    for (a, cz, vd), n in sorted(ab.items()):
        S.append(f"| {a} | {cz} | {vd} | {n} |")

    # per-task averaged score on common task sets
    S += ["\n## Fair comparison on the tasks both arms have a verdict for\n",
          "Per-task score = mean over that arm's valid runs; arm score = mean of task scores over the common tasks. Differences in run counts per task do not bias it.\n",
          "| pair | common tasks | score A | score B | A−B (pp) | tasks only A solves | tasks only B solves |", "|---|---|---|---|---|---|---|"]
    pairs = [("sonnet5-old", "sonnet5-new"), ("astra-old", "astra-new"), ("sonnet5-new", "astra-new"), ("sonnet5-old", "astra-old"),
             ("sonnet46-old", "sonnet5-old"), ("sonnet46-old", "sonnet5-new")]
    for x, y in pairs:
        common = [t for t in tasks if task_score(x, t) is not None and task_score(y, t) is not None]
        if not common:
            S.append(f"| {x} vs {y} | 0 | | | | | |")
            continue
        sx = statistics.mean(task_score(x, t) for t in common)
        sy = statistics.mean(task_score(y, t) for t in common)
        ox = sum(1 for t in common if task_score(x, t) > 0 and task_score(y, t) == 0)
        oy = sum(1 for t in common if task_score(y, t) > 0 and task_score(x, t) == 0)
        S.append(f"| {x} vs {y} | {len(common)} | {100*sx:.1f}% | {100*sy:.1f}% | {100*(sx-sy):+.1f} | {ox} | {oy} |")

    # by bucket
    S += ["\n## Pass rate by app bucket (valid runs, main campaigns)\n"]
    buckets = sorted({r["bucket"] for r in main_runs})
    S.append("| bucket | " + " | ".join(ARMS) + " |")
    S.append("|---|" + "---|" * len(ARMS))
    for b in buckets:
        row = []
        for a in ARMS:
            v = [r for r in valid(by_arm[a]) if r["bucket"] == b]
            row.append(f"{pct(sum(r['verdict']=='SUCCESS' for r in v), len(v))} ({len(v)})" if v else "—")
        S.append(f"| {b} | " + " | ".join(row) + " |")

    # failure categories
    S += ["\n## Why runs did not pass (all non-PASS runs, main campaigns)\n",
          "Cause labels are rule-based (see METHODOLOGY.md); hand-verified findings are in FINDINGS.md and the per-task pages.\n"]
    causes = sorted({r["cause"] for r in main_runs if r["outcome"] != "PASS"})
    S.append("| cause | " + " | ".join(ARMS) + " |")
    S.append("|---|" + "---|" * len(ARMS))
    for cz in causes:
        row = []
        for a in ARMS:
            n = sum(1 for r in by_arm[a] if r["cause"] == cz)
            tot = sum(1 for r in by_arm[a] if r["outcome"] != "PASS")
            row.append(f"{n} ({pct(n, tot)})" if n else "—")
        S.append(f"| {cz} | " + " | ".join(row) + " |")

    # mode / factor marginals
    S += ["\n### Failure mode (what the agent/harness did) — FAILURE runs only\n", "| mode | " + " | ".join(ARMS) + " |", "|---|" + "---|" * len(ARMS)]
    fails = lambda a: [r for r in by_arm[a] if r["verdict"] == "FAILURE"]
    for mo in sorted({r["mode"] for a in ARMS for r in fails(a)}):
        S.append(f"| {mo} | " + " | ".join((lambda n, t: f"{n} ({pct(n, t)})" if n else "—")(sum(1 for r in fails(a) if r["mode"] == mo), len(fails(a))) for a in ARMS) + " |")
    S += ["\n### Environment factor present in the failure — FAILURE runs only\n",
          "A factor is a condition that plausibly blocked the agent (not proof it was the sole cause; an agent can sometimes work around it, e.g. by using a terminal): `black-desktop` = the guest shows a bare black Openbox session (no GNOME shell/Settings/panel) and the agent could not proceed (declared FAIL / hit the cap) — tasks stay solvable from a terminal, so it is a handicap, not always a blocker; `site-blocked` = live site refused access; `missing-precondition` = agent reports a missing file/app/account the task assumes; `tool-http-error` = ≥3 desktop-tool (non-run_python) calls returned HTTP/connection errors. Factors are attributed only to runs that gave up or hit the cap, never to runs that finished or refused.\n",
          "| factor | " + " | ".join(ARMS) + " |", "|---|" + "---|" * len(ARMS)]
    for fa in [None, "black-desktop", "site-blocked", "missing-precondition", "tool-http-error"]:
        S.append(f"| {fa or 'none detected'} | " + " | ".join((lambda n, t: f"{n} ({pct(n, t)})" if n else "—")(sum(1 for r in fails(a) if r["factor"] == fa), len(fails(a))) for a in ARMS) + " |")

    # infeasibility handling
    inf_tasks = {r["task_id"] for r in runs if "infeasible" in (r["reason"] or "")}
    S += [f"\n## Infeasible-task handling ({len(inf_tasks)} tasks whose official check is 'infeasible')\n",
          "Declaring FAIL on an infeasible task is correct; declaring FAIL on a feasible task is giving up. `declared FAIL` counts runs with answer FAIL / [INFEASIBLE].\n",
          "| arm | valid runs on infeasible tasks | correctly declared | missed (claimed done) | valid runs on feasible tasks | declared FAIL on feasible (gave up) | give-up rate |", "|---|---|---|---|---|---|---|"]
    isfail = lambda r: (r["answer"] or "").strip().upper() == "FAIL" or "[INFEASIBLE]" in (r["final_msg"] or "").upper()
    for a in ARMS:
        v = valid(by_arm[a])
        vi = [r for r in v if r["task_id"] in inf_tasks]
        vf = [r for r in v if r["task_id"] not in inf_tasks]
        gu = sum(1 for r in vf if isfail(r))
        S.append(f"| {a} | {len(vi)} | {sum(r['verdict']=='SUCCESS' for r in vi)} | {sum(r['verdict']=='FAILURE' for r in vi)} | {len(vf)} | {gu} | {pct(gu, len(vf))} |")

    # consistency
    S += ["\n## Run-to-run consistency (tasks with ≥2 valid runs)\n", "| arm | tasks | all runs pass | mixed (flaky) | all runs fail |", "|---|---|---|---|---|"]
    for a in ARMS:
        tt = [(t, cell[(t, a)]) for (t, aa) in cell if aa == a and cell[(t, aa)][0] >= 2]
        S.append(f"| {a} | {len(tt)} | {sum(1 for _, c in tt if c[1] == c[0])} | {sum(1 for _, c in tt if 0 < c[1] < c[0])} | {sum(1 for _, c in tt if c[1] == 0)} |")

    # arm x bucket csv + failures by checker csv
    with open(OUT / "tables/arm_bucket_metrics.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm", "bucket", "valid_runs", "pass", "pass_pct", "median_wall_s", "median_agent_s", "median_steps", "median_cost_usd", "total_cost_usd"])
        for a in ARMS:
            for b in sorted({r["bucket"] for r in by_arm[a]}):
                v = [r for r in valid(by_arm[a]) if r["bucket"] == b]
                if not v:
                    continue
                cs = [r["cost_usd"] for r in v if r["cost_usd"] is not None]
                w.writerow([a, b, len(v), sum(r["verdict"] == "SUCCESS" for r in v), round(100 * sum(r["verdict"] == "SUCCESS" for r in v) / len(v), 1), med([r["wall_s"] for r in v]), med([r["agent_s"] for r in v]), med([r["steps"] for r in v]), round(statistics.median(cs), 3) if cs else "", round(sum(cs), 2) if cs else ""])
    with open(OUT / "tables/failures_by_checker.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm", "checker", "valid_runs", "failures", "fail_pct"])
        for a in ARMS:
            cnt = collections.defaultdict(lambda: [0, 0])
            for r in valid(by_arm[a]):
                k = r["checker"] or "(harness/other)"
                cnt[k][0] += 1
                cnt[k][1] += r["verdict"] == "FAILURE"
            for k, (n, fl) in sorted(cnt.items(), key=lambda kv: -kv[1][1]):
                w.writerow([a, k, n, fl, round(100 * fl / n, 1)])

    # timing / tokens / cost
    S += ["\n## Time, steps, tokens, cost (main campaigns, runs with a valid verdict)\n",
          "Total input tokens = fresh + cache-read + cache-write for Claude (so cached context counts), `input_tokens` as reported (cached included) for Codex. `wall` = started→finished of the attempt recorded in provenance; `agent` = Claude `duration_ms` / transcript timestamps (not available for Codex arms). Medians; mean and p90 in the CSV.\n",
          "| arm | outcome | n | median wall s | median agent s | median steps | median total in-tok | median out-tok | median cost $ | total cost $ |", "|---|---|---|---|---|---|---|---|---|---|"]
    trows = []
    for a in ARMS:
        for oc, flt in (("all valid", lambda r: True), ("pass", lambda r: r["verdict"] == "SUCCESS"), ("fail", lambda r: r["verdict"] == "FAILURE")):
            v = [r for r in valid(by_arm[a]) if flt(r)]
            if not v:
                continue
            costs = [r["cost_usd"] for r in v if r["cost_usd"] is not None]
            medc = round(statistics.median(costs), 3) if costs else None
            totc = round(sum(costs), 1) if costs else None
            S.append(f"| {a} | {oc} | {len(v)} | {fmtn(med([r['wall_s'] for r in v]))} | {fmtn(med([r['agent_s'] for r in v]))} | {fmtn(med([r['steps'] for r in v]))} | {fmtn(med([r['total_in_tok'] for r in v]))} | {fmtn(med([r['out_tok'] for r in v]))} | {fmtn(medc, 3) if medc is not None else '—'} | {fmtn(totc, 1) if totc is not None else '—'} |")
            trows.append([a, oc, len(v), mean([r["wall_s"] for r in v]), med([r["wall_s"] for r in v]), p90([r["wall_s"] for r in v]), mean([r["agent_s"] for r in v]), med([r["agent_s"] for r in v]), p90([r["agent_s"] for r in v]), mean([r["steps"] for r in v]), med([r["steps"] for r in v]), p90([r["steps"] for r in v]), totc])
    with open(OUT / "tables/timing_by_arm.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm", "outcome", "n", "wall_mean_s", "wall_median_s", "wall_p90_s", "agent_mean_s", "agent_median_s", "agent_p90_s", "steps_mean", "steps_median", "steps_p90", "total_cost_usd"])
        w.writerows(trows)
    # cost per pass
    S += ["\n### Cost per successful run\n", "| arm | total cost $ (valid runs) | passes | $ per pass | cost kind |", "|---|---|---|---|---|"]
    for a in ARMS:
        v = valid(by_arm[a])
        tot = sum(r["cost_usd"] or 0 for r in v)
        ps = sum(r["verdict"] == "SUCCESS" for r in v)
        kinds = collections.Counter(r["cost_kind"] for r in v)
        S.append(f"| {a} | {tot:,.1f} | {ps} | {tot/ps:,.2f} | {dict(kinds)} |" if ps else f"| {a} | {tot:,.1f} | 0 | — | {dict(kinds)} |")

    # infra friction
    S += ["\n## Infrastructure friction (main campaigns)\n", "| arm | runs with ≥1 infra retry | total retry records | RATE_LIMITED | INFRA_FLAKE | other |", "|---|---|---|---|---|---|"]
    for a in ARMS:
        rs = by_arm[a]
        agg = collections.Counter()
        for r in rs:
            for k, n in (r["infra_types"] or {}).items():
                agg[k] += n
        tot = sum(agg.values())
        S.append(f"| {a} | {sum(r['infra_attempts'] > 0 for r in rs)} / {len(rs)} | {tot} | {agg['RATE_LIMITED']} | {agg['INFRA_FLAKE']} | {tot - agg['RATE_LIMITED'] - agg['INFRA_FLAKE']} |")

    # tool-use profile
    S += ["\n## Tool-use profile (main campaigns, mean calls per run with a transcript)\n",
          "Old Claude harness exposed many tools (click/type/key/run_python/a11y_tree...); the official protocol exposes one `computer` tool. `non-osworld` = any tool outside the OSWorld MCP server (ToolSearch, Bash, Read...).\n"]
    tool_cols = ["screenshot", "click", "key", "type", "run_python", "a11y_tree", "computer", "non-osworld"]
    S.append("| arm | runs | " + " | ".join(tool_cols) + " |")
    S.append("|---|---|" + "---|" * len(tool_cols))
    for a in ARMS:
        rs = [r for r in by_arm[a] if r["has_conv"]]
        if not rs:
            continue
        agg = collections.Counter()
        for r in rs:
            for k, n in r["tools_used"].items():
                key = k.replace("mcp__osworld__", "")
                if key == "computer" or k == "computer":
                    agg["computer"] += n
                elif k.startswith("mcp__osworld__"):
                    agg[key] += n
                elif not k.startswith("other:"):
                    agg["non-osworld"] += n
        S.append(f"| {a} | {len(rs)} | " + " | ".join(f"{agg[c]/len(rs):.1f}" for c in tool_cols) + " |")

    S += ["\n## Transcript integrity (main campaigns)\n",
          "`lost` = the transcript was overwritten by a later aborted retry (it only contains a quota/login error) while eval.json/final.png come from the earlier real attempt: the verdict is genuine, the agent behaviour is not recoverable. `after eval` = transcript newer than the verdict by >10 min (verdict and transcript may belong to different attempts).\n",
          "| arm | runs | with transcript | lost | transcript written >10 min after eval | within 10 min | no transcript |", "|---|---|---|---|---|---|---|"]
    for a in ARMS:
        rs = by_arm[a]
        S.append(f"| {a} | {len(rs)} | {sum(r['has_conv'] for r in rs)} | {sum(r['transcript_lost'] for r in rs)} | {sum(1 for r in rs if r['transcript_after_eval'] and not r['transcript_lost'])} | {sum(1 for r in rs if r['has_conv'] and not r['transcript_after_eval'])} | {sum(1 for r in rs if not r['has_conv'])} |")

    # model-family comparison on tasks
    claude = ["sonnet46-old", "sonnet5-old", "sonnet5-new"]
    codex = ["astra-old", "astra-old-openbook", "astra-new"]
    def fam(tid, arms_):
        v = sum(cell.get((tid, a), [0, 0])[0] for a in arms_)
        p = sum(cell.get((tid, a), [0, 0])[1] for a in arms_)
        return None if not v else p > 0
    S += ["\n## Claude vs Codex family, per task (informative runs, any run passes)\n", "| scope | tasks with data in both families | solved by both | only Claude | only Codex | neither |", "|---|---|---|---|---|---|"]
    for label, ca_, co_ in (("all arms", claude, codex), ("new infrastructure only", ["sonnet5-new"], ["astra-new"]), ("old infrastructure only", ["sonnet46-old", "sonnet5-old"], ["astra-old", "astra-old-openbook"])):
        both = only_c = only_x = neither = n = 0
        for tid in tasks:
            fc, fx = fam(tid, ca_), fam(tid, co_)
            if fc is None or fx is None:
                continue
            n += 1
            both += fc and fx
            only_c += fc and not fx
            only_x += fx and not fc
            neither += (not fc) and (not fx)
        S.append(f"| {label} | {n} | {both} | {only_c} | {only_x} | {neither} |")

    # old -> new infrastructure flips
    def agg(tid, arms_):
        v = sum(cell.get((tid, a), [0, 0])[0] for a in arms_)
        pz = sum(cell.get((tid, a), [0, 0])[1] for a in arms_)
        return pz, v
    old_arms, new_arms = ["sonnet46-old", "sonnet5-old", "astra-old"], ["sonnet5-new", "astra-new"]
    reg, gain = [], []
    for tid, t in tasks.items():
        op, ov = agg(tid, old_arms)
        np_, nv = agg(tid, new_arms)
        if ov >= 3 and nv >= 3:
            if op / ov >= 0.66 and np_ == 0:
                reg.append((t["bucket"], tid, op, ov, np_, nv, t["instruction"]))
            if op == 0 and np_ / nv >= 0.66:
                gain.append((t["bucket"], tid, op, ov, np_, nv, t["instruction"]))
    S += ["\n## Old → new infrastructure: tasks that flip (≥3 informative runs on each side)\n",
          "Regression = ≥66% pass in the old arms and 0 passes in the new arms; gain = 0 passes in the old arms and ≥66% in the new ones. A regression on a task where the *same GUI sequence* passed in Phase 1 points at the infrastructure, not at the model.\n",
          f"**{len(reg)} regressions, {len(gain)} gains** (gains by bucket: {dict(collections.Counter(g[0] for g in gain))}).\n",
          "| regression | bucket | old pass/runs | new pass/runs | instruction |", "|---|---|---|---|---|"]
    for b, tid, op, ov, np_, nv, ins in sorted(reg):
        S.append(f"| {tid[:8]} | {b} | {op}/{ov} | {np_}/{nv} | {(ins or '')[:80]} |")
    S += ["", "| gain | bucket | old pass/runs | new pass/runs | instruction |", "|---|---|---|---|---|"]
    for b, tid, op, ov, np_, nv, ins in sorted(gain):
        S.append(f"| {tid[:8]} | {b} | {op}/{ov} | {np_}/{nv} | {(ins or '')[:80]} |")

    # verification of automatic labels
    if VERIF:
        S += ["\n## Verification of the automatic labels (manual sample)\n", VERIF["_about"], "",
              "| arm | cause | sampled | correct | partial | wrong | note |", "|---|---|---|---|---|---|---|"]
        for v in VERIF["round1"]:
            S.append(f"| {v['arm']} | {v['cause']} | {v['n']} | {v['ok']} | {v['partial']} | {v['wrong']} | {v['note']} |")
        tot = sum(v["n"] for v in VERIF["round1"])
        S.append(f"\nRound 1 totals: {tot} runs read — {sum(v['ok'] for v in VERIF['round1'])} correct, {sum(v['partial'] for v in VERIF['round1'])} partial, {sum(v['wrong'] for v in VERIF['round1'])} wrong. Fixes applied afterwards:\n")
        for fx in VERIF["fixes_applied_after_round1"]:
            S.append(f"- {fx}")
        for fx in VERIF.get("fixes_applied_after_round2", []):
            S.append(f"- (after round 2) {fx}")
        for rnd in ("round2",):
            if VERIF.get(rnd):
                S += [f"\n### {rnd}\n", "| arm | cause | sampled | correct | partial | wrong | note |", "|---|---|---|---|---|---|---|"]
                for v in VERIF[rnd]:
                    S.append(f"| {v['arm']} | {v['cause']} | {v['n']} | {v['ok']} | {v['partial']} | {v['wrong']} | {v['note']} |")

    # all campaigns
    S += ["\n## Every campaign tree\n", "| arm | variant | tree | tasks | run dirs | valid | pass | fail | oracle | infra | no verdict |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for tree, root, arm, variant, main_ in CAMPAIGNS:
        rs = [r for r in runs if r["campaign"] == tree]
        if not rs:
            continue
        c = collections.Counter(r["verdict"] for r in rs)
        S.append(f"| {arm} | {variant}{' *' if main_ else ''} | `{tree}` | {len({r['task_id'] for r in rs})} | {len(rs)} | {c['SUCCESS'] + c['FAILURE']} | {c['SUCCESS']} | {c['FAILURE']} | {c['EVAL_ERROR']} | {c['ENVIRONMENT_ERROR']} | {c[None]} |")
    S.append("\n`*` = main campaign of the arm (used in headline numbers).")

    # manual notes table
    if NOTES:
        S += ["\n## Hand-verified findings (see FINDINGS.md and tasks/<bucket>/<id>.md)\n", "| task | bucket | category | confidence |", "|---|---|---|---|"]
        for tid, t in sorted(tasks.items(), key=lambda kv: (kv[1]["bucket"], kv[0])):
            if tid[:8] in NOTES:
                S.append(f"| {tid[:8]} | {t['bucket']} | {NOTES[tid[:8]]['cat']} | {NOTES[tid[:8]]['conf']} |")
    # never solved
    never = [t for t in tasks.values() if any(cell.get((t["task_id"], a), [0, 0])[0] for a in ARMS) and not any(cell.get((t["task_id"], a), [0, 0])[1] for a in ARMS)]
    S += [f"\n## Tasks never solved by any arm ({len(never)})\n", "Strong indicator of an environment, checker or specification problem shared by all models. Full list with per-arm counts: `tables/task_matrix.csv`.\n",
          "| task | bucket | tried-by (valid runs) | instruction |", "|---|---|---|---|"]
    for t in sorted(never, key=lambda t: (t["bucket"], t["task_id"])):
        tb = ", ".join(f"{a}:{cell[(t['task_id'], a)][0]}" for a in ARMS if cell.get((t["task_id"], a), [0, 0])[0])
        S.append(f"| {t['task_id'][:8]} | {t['bucket']} | {tb} | {(t['instruction'] or '')[:90]} |")
    (OUT / "SUMMARY.md").write_text("\n".join(S))

    # ---- per-task cross-arm pages
    (OUT / "tasks").mkdir(exist_ok=True)
    by_task = collections.defaultdict(list)
    for r in runs:
        by_task[r["task_id"]].append(r)
    for tid, rs in by_task.items():
        t = tasks[tid]
        d = OUT / "tasks" / t["bucket"]
        d.mkdir(parents=True, exist_ok=True)
        P = [f"# Task `{tid}`", f"**Bucket:** {t['bucket']}  \n**Instruction:** {t['instruction']}\n"]
        if tid[:8] in NOTES:
            n = NOTES[tid[:8]]
            P.append(f"> **Manual review ({n['cat']}, confidence {n['conf']}):** {n['note']}\n")
        P += ["| arm | main campaign valid runs | pass |", "|---|---|---|"]
        for a in ARMS:
            v, p = cell.get((tid, a), [0, 0])
            P.append(f"| {a} | {v} | {p} |")
        for a in ARMS:
            ars = [r for r in rs if r["arm"] == a]
            if ars:
                P.append(f"\n## {a}")
                for r in sorted(ars, key=lambda r: (not r["main"], r["campaign"], r["run"])):
                    P.append(md_run(r))
        (d / f"{tid}.md").write_text("\n".join(P))
    print("runs", len(runs), "tasks", len(tasks))


main()
