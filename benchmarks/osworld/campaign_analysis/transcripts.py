"""One reader for both runtimes' saved transcripts. Zero quota.

    from transcripts import calls
    for c in calls(run_dir): ...   # {"tool", "args", "image", "text"}

The two CLIs write different things and the analyses kept re-parsing them ad hoc. F2's no-op
detector read the Codex format only, so Sonnet was uncovered; X10's mechanism counts need the
agent's own typed text for both. This module is the single place that knows the difference.

  Codex (`codex exec --json`): one JSON object per line; a call is an `item.completed` event whose
  item is an `mcp_tool_call`, with `arguments` (sometimes a JSON string) and a `result.content`
  list that carries the screenshot as a base64 `image` part.

  Claude Code (`claude -p`): one JSON object per line; a call is an `assistant` message block of
  type `tool_use` (name, input, id), and its screenshot arrives in the following `user` message as
  a `tool_result` block with the same id, whose content holds an `image` part with
  `source.data`.

What the caller gets is the same in both cases, in call order, so an analysis never has to branch
on the runtime again.
"""
import json
from pathlib import Path

COMPUTER = "computer"


def _codex(path):
    for line in path.open(errors="replace"):
        if '"mcp_tool_call"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") != "item.completed" or item.get("type") != "mcp_tool_call":
            continue
        args = item.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        image = text = None
        for part in ((item.get("result") or {}).get("content") or []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image" and part.get("data") and image is None:
                image = part["data"]
            elif part.get("type") == "text":
                text = (text or "") + part.get("text", "")
        yield {"tool": (item.get("tool") or "").replace("mcp__osworld__", ""),
               "args": args if isinstance(args, dict) else {}, "image": image, "text": text}


def _claude(path):
    pending, order = {}, []
    out = {}
    for line in path.open(errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") not in ("assistant", "user"):
            continue
        content = (ev.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if ev["type"] == "assistant" and b.get("type") == "tool_use":
                cid = b.get("id")
                pending[cid] = {"tool": (b.get("name") or "").replace("mcp__osworld__", ""),
                                "args": b.get("input") if isinstance(b.get("input"), dict) else {},
                                "image": None, "text": None}
                order.append(cid)
            elif ev["type"] == "user" and b.get("type") == "tool_result":
                rec = pending.get(b.get("tool_use_id"))
                if rec is None:
                    continue
                cc = b.get("content")
                for x in (cc if isinstance(cc, list) else []):
                    if not isinstance(x, dict):
                        continue
                    if x.get("type") == "image" and rec["image"] is None:
                        rec["image"] = (x.get("source") or {}).get("data")
                    elif x.get("type") == "text":
                        rec["text"] = (rec["text"] or "") + x.get("text", "")
                out[b["tool_use_id"]] = rec
    for cid in order:
        rec = out.get(cid) or pending.get(cid)
        if rec:
            yield rec


def runtime(run_dir):
    """'codex' or 'claude', from the tree name the run sits in."""
    return "codex" if "gpt6astra" in str(run_dir) else "claude"


def calls(run_dir, tool=None):
    """Every tool call of a run, in order, with its screenshot when the runtime returned one."""
    path = Path(run_dir) / "conversation.jsonl"
    if not path.exists():
        return
    reader = _codex if runtime(run_dir) == "codex" else _claude
    for c in reader(path):
        if tool is None or c["tool"] == tool:
            yield c


def sub_actions(call):
    """A `computer` call's actions: the batch when there is one, otherwise the call itself."""
    acts = call["args"].get("actions")
    if isinstance(acts, list):
        return [a for a in acts if isinstance(a, dict)]
    return [call["args"]] if call["args"].get("action") else []


def typed_text(run_dir):
    """Everything the agent typed into the desktop, plus every shell command it ran.

    This is what a mechanism count must look at: the system prompt also contains the word
    `gsettings` under the card arms, so counting on the transcript as a whole would count the
    prompt rather than the behaviour (the trap X5 names explicitly).
    """
    out = []
    for c in calls(run_dir):
        if c["tool"] == COMPUTER:
            out += [a.get("text") or "" for a in sub_actions(c) if a.get("action") == "type"]
        elif c["tool"] == "run_command":
            out.append(c["args"].get("command") or "")
    log = Path(run_dir) / "commands.jsonl"
    if log.exists():
        for line in log.open(errors="replace"):
            try:
                out.append(json.loads(line).get("command") or "")
            except ValueError:
                pass
    return [t for t in out if t]
