#!/usr/bin/env python3
"""Lastline — the hook cuts one line out of an edit and makes the human write it.

Hooks are setsid'd with no controlling tty, so input cannot come from here (spike 1).
Instead the hook denies the edit and instructs the agent to relay: ask the human, then
re-apply with their line. The hook keeps the original and grades what comes back.
"""
import datetime
import difflib
import json
import os
import re
import sys
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "lastline.log")
BUDGET_FILE = os.path.join(HERE, ".budget.json")
DAILY_BUDGET = int(os.environ.get("LASTLINE_BUDGET", "3"))
PENDING_TTL = 600  # a puzzle the agent never resolved is dead after 10 min
COOLDOWN = int(os.environ.get("LASTLINE_COOLDOWN", "900"))
MODEL = os.environ.get("LASTLINE_MODEL", "claude-opus-5")
PICK_TIMEOUT = int(os.environ.get("LASTLINE_PICK_TIMEOUT", "45"))

LANGS = {".py": ("python", "#"), ".js": ("javascript", "//"), ".ts": ("typescript", "//"),
         ".tsx": ("tsx", "//"), ".jsx": ("jsx", "//"), ".go": ("go", "//"),
         ".rs": ("rust", "//"), ".java": ("java", "//"), ".kt": ("kotlin", "//"),
         ".rb": ("ruby", "#"), ".sh": ("bash", "#"), ".sql": ("sql", "--"),
         ".c": ("c", "//"), ".h": ("c", "//"), ".cpp": ("cpp", "//"), ".php": ("php", "//"),
         ".swift": ("swift", "//"), ".scala": ("scala", "//"), ".lua": ("lua", "--"),
         ".yaml": ("yaml", "#"), ".yml": ("yaml", "#"), ".tf": ("hcl", "#")}

SKIP_FILES = re.compile(r"(lock|\.min\.|\.lock$|/(dist|build|node_modules|vendor)/|\.snap$)")


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")


def respond(decision, reason, system=None):
    log(f"{decision}: {reason.splitlines()[0][:120]}")
    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}
    if system:
        out["systemMessage"] = system  # goes to the human, not through the agent
    print(json.dumps(out))
    sys.exit(0)


def allow(reason="pass", system=None):
    respond("allow", reason, system)


# ---------- state ----------

def state_path(session_id):
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "nosession")[:64]
    return os.path.join(tempfile.gettempdir(), f"lastline-{sid}.json")


def load_state(session_id):
    try:
        with open(state_path(session_id)) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(session_id, state):
    with open(state_path(session_id), "w") as f:
        json.dump(state, f)


def budget_left():
    today = datetime.date.today().isoformat()
    try:
        b = json.load(open(BUDGET_FILE))
    except Exception:
        b = {}
    if b.get("date") != today:
        b = {"date": today, "spent": 0}
    return b, DAILY_BUDGET - b["spent"]


def spend():
    b, _ = budget_left()
    b["spent"] += 1
    b["last_fire"] = datetime.datetime.now().timestamp()
    json.dump(b, open(BUDGET_FILE, "w"))


def in_cooldown() -> bool:
    """Two puzzles back to back is an interrogation, not a moment."""
    b, _ = budget_left()
    since = datetime.datetime.now().timestamp() - b.get("last_fire", 0)
    return since < COOLDOWN


# ---------- picking ----------

def added_lines(old, new):
    """(index in new_string, text) for every line the edit introduces."""
    new_lines = new.splitlines()
    sm = difflib.SequenceMatcher(None, old.splitlines(), new_lines)
    out = []
    for tag, _, _, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            out.extend((j, new_lines[j]) for j in range(j1, j2))
    return out


PICK_PROMPT = """You are choosing one line of a code change for a human to write themselves.

Pick a line where a competent engineer could reasonably have written something DIFFERENT,
and the difference would matter. The test is not "is this line important" - it is "was
there a real fork here". A boundary that could defensibly go either way, a branch that
could handle the error differently, an ordering that could be reversed.

Reject a line if:
- there is only one sensible way to write it
- writing it needs recall of an exact name, signature, argument order or literal
- it is an import, a log, a comment, boilerplate, or fixed by the line above it

Proof that a fork exists: you must supply `alternative`, a different line a good engineer
might plausibly have written instead - AND the two must behave observably differently.
A different spelling is not a fork. Nudging a comparison by one only counts when the one
is worth something:
  `attempt <= max_retries` vs `<` - counts, that is a whole extra retry
  `elapsed <= COOLDOWN` vs `<`  - does not count, that is a microsecond
If your alternative would not change what anyone sees, there was no decision - return
{"line": null}.

Reply with JSON only, no prose:
{"line": <0-based index into AFTER>, "why": "<8 words max>", "alternative": "<the other line>"}
or {"line": null}

FILE: %s

BEFORE:
%s

AFTER (indexed):
%s
"""


def pick_model(ti, candidates):
    """Ask a model which line holds a real decision. Returns (idx, text) or None."""
    lines = ti["new_string"].splitlines()
    eligible = {i for i, _ in candidates}
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(lines) if i in eligible)
    prompt = PICK_PROMPT % (ti.get("file_path", "?"), ti.get("old_string", "")[:4000], numbered[:6000])
    try:
        r = subprocess.run(["claude", "-p", prompt, "--model", MODEL, "--output-format", "text"],
                           capture_output=True, text=True, timeout=PICK_TIMEOUT)
        raw = r.stdout.strip()
        start = raw.find("{")
        # raw_decode stops at the first complete object; trailing prose is common
        data, _ = json.JSONDecoder().raw_decode(raw[start:]) if start >= 0 else ({}, 0)
    except Exception as e:
        log(f"picker failed {e!r}")
        return None
    idx = data.get("line")
    if not isinstance(idx, int) or idx not in eligible:
        log(f"picker declined ({data.get('line')!r})")
        return None
    log(f"picker chose {idx}: {data.get('why')!r} alt={data.get('alternative')!r}")
    return idx, lines[idx], (data.get("alternative") or "")


# ---------- grading ----------

def norm(s):
    return re.sub(r"\s+", " ", s.strip())


CMP = re.compile(r"<=|>=|==|!=|<|>")


def survived(entry) -> bool:
    """Did their line outlast the session? Line numbers drift, so match on content."""
    try:
        body = open(entry["file"]).read()
    except OSError:
        return False
    return any(norm(l) == entry["line"] for l in body.splitlines())


def verdict(theirs, ours):
    if norm(theirs) == norm(ours):
        return "same"
    # an off-by-one is never "close" - it is the whole point (design.md §4)
    if CMP.findall(theirs) != CMP.findall(ours):
        return "different"
    if difflib.SequenceMatcher(None, norm(theirs), norm(ours)).ratio() >= 0.75:
        return "close"
    return "different"


# ---------- the two halves ----------

DEFN = re.compile(r"^\s*(?:async\s+)?(?:def|class|func|function|fn|sub)\s+([A-Za-z_]\w*)"
                  r"|^\s*(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(")


def enclosing_name(lines, idx):
    for i in range(idx, -1, -1):
        m = DEFN.match(lines[i])
        if m:
            return m.group(1) or m.group(2)
    return None


def call_sites(name, skip_path, limit=6):
    """A blank is only writable if you can see what uses it (dogfood, 2026-09-17)."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        tracked = subprocess.run(["git", "-C", root, "ls-files"],
                                 capture_output=True, text=True, timeout=3).stdout.split()
        if not tracked:
            return []
        out = subprocess.run(["grep", "-nI", f"\\b{name}\\b"] + tracked,
                             capture_output=True, text=True, timeout=3, cwd=root).stdout
    except Exception:
        return []
    hits = []
    for ln in out.splitlines():
        f, _, rest = ln.partition(":")
        if os.path.abspath(os.path.join(root, f)) == os.path.abspath(skip_path):
            continue
        n, _, text = rest.partition(":")
        if DEFN.match(text):
            continue
        hits.append(f"{f}:{n}  {text.strip()[:90]}")
        if len(hits) >= limit:
            break
    return hits


def puzzle(ti, idx, line):
    """Before/after, because a hole with no intent is unwritable (dogfood, 2026-09-17)."""
    path = ti.get("file_path", "?")
    lang, cmt = LANGS.get(os.path.splitext(path)[1], ("", "#"))
    lines = ti["new_string"].splitlines()
    after = []
    for i, text in enumerate(lines[:60]):
        if i == idx:
            indent = re.match(r"\s*", text).group()
            after.append(f"{indent}____________________  {cmt} <- you")
        else:
            after.append(text)
    before = ti.get("old_string", "").splitlines()[:60] or ["(new file)"]
    name = enclosing_name(lines, idx)
    sites = call_sites(name, path) if name else []
    if name and sites:
        used = "\n\nwhere `{}` is used\n```\n{}\n```".format(name, "\n".join(sites))
    elif name:
        used = f"\n\n`{name}` is not referenced anywhere else in the project."
    else:
        used = ""
    return f"""LASTLINE is holding this edit. Do not retry it as-is.

Ask the human to write one line of it themselves. Show them this and nothing else:

`{path}`

before
```{lang}
{chr(10).join(before)}
```

after
```{lang}
{chr(10).join(after)}
```

{used}

Relay all of the above exactly as it is, fences and language tag included, so
they render highlighted. Above them, give ONE short sentence saying what the whole edit is for. Change-level
only - the goal you were asked to achieve. This is the only context they get.

Rules:
- Do not write the line for them. Hints are fine - typing it is the point, not guessing it.
- Ask once, then stop and wait for their answer.
- When they answer, re-apply this exact Edit with their line in place of the blank,
  changing nothing else.
- If they say skip or decline, re-apply this exact Edit unchanged.
"""


def resolve(pending, ti, session_id, state):
    ours = pending["line"]
    theirs_all = ti["new_string"].splitlines()
    idx = pending["idx"]
    theirs = theirs_all[idx] if idx < len(theirs_all) else ""

    # the agent must have changed that line and nothing else
    a = pending["new_string"].splitlines()
    b = list(theirs_all)
    tampered = len(a) != len(b) or [i for i in range(min(len(a), len(b))) if a[i] != b[i]] not in ([], [idx])
    if tampered and norm(theirs) != norm(ours):
        state["pending"]["at"] = datetime.datetime.now().timestamp()
        save_state(session_id, state)
        respond("deny", "The rest of the edit changed. Re-apply the original with only "
                        "their line swapped in, nothing else.")

    v = verdict(theirs, ours)
    state.pop("pending", None)
    if v == "different":
        state.setdefault("standing", []).append(
            {"file": ti["file_path"], "line": norm(theirs), "idx": idx})
    save_state(session_id, state)
    log(f"resolved {v} tampered={tampered} theirs={norm(theirs)!r} ours={norm(ours)!r}")

    mark = {"same": "\n    \u2022 same", "close": "\n    \u2022 close", "different": ""}[v]
    rows = [f"    yours   {norm(theirs)}", f"    mine    {norm(ours)}"]
    alt = norm(pending.get("alt", ""))
    # the fork the picker saw - it is what makes a match mean anything
    if alt and alt not in (norm(theirs), norm(ours)):
        rows.append(f"    or      {alt}")
    reveal = "  lastline\n" + "\n".join(rows) + mark
    tamper_note = "\n(NOTE: the rest of the edit changed too. Tell them.)" if tampered else ""
    agent = f"""Allowed. Their line is in, and they have already been shown both versions -
do not repeat them.

Their line is what shipped. It is intent, not a draft: if you think it is wrong, say so as
an objection and let them decide. Do not quietly edit it back.{tamper_note}
"""
    return agent, reveal


def on_stop(payload):
    """Session over. A line of theirs still standing is the only loud moment."""
    session_id = payload.get("session_id", "")
    state = load_state(session_id)
    still = [e for e in state.get("standing", []) if survived(e)]
    try:
        os.remove(state_path(session_id))
    except OSError:
        pass
    if not still:
        sys.exit(0)
    rows = "\n".join(f"    {os.path.basename(e['file'])}   {e['line']}" for e in still)
    print(json.dumps({"systemMessage": f"  lastline - still standing\n{rows}"}))
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow("unreadable payload")

    if payload.get("hook_event_name") == "Stop":
        on_stop(payload)

    ti = payload.get("tool_input", {})
    session_id = payload.get("session_id", "")
    path = ti.get("file_path", "")
    state = load_state(session_id)

    pending = state.get("pending")
    fresh = bool(pending) and (datetime.datetime.now().timestamp() - pending["at"]) < PENDING_TTL
    if fresh and pending["file_path"] == path and pending["old_string"] == ti.get("old_string"):
        agent_text, reveal = resolve(pending, ti, session_id, state)
        allow(agent_text, system=reveal)
    if pending and not fresh:
        state.pop("pending", None)
        save_state(session_id, state)
        log("pending expired")

    if SKIP_FILES.search(path) or not path:
        allow("skipped file")
    cands = added_lines(ti.get("old_string", ""), ti.get("new_string", ""))
    if len(cands) < 2:
        allow(f"too small ({len(cands)} added)")

    if fresh:
        allow("a puzzle is already pending")  # stacking them loses the first

    # gate before the model call, or it is fifty calls a day to spend three
    _, left = budget_left()
    if in_cooldown():
        allow("cooling down")
    if left <= 0:
        allow("budget spent")

    chosen = pick_model(ti, cands)
    if chosen is None:
        allow("no decision line")

    idx, line, alt = chosen
    spend()
    state["pending"] = {
        "file_path": path, "old_string": ti.get("old_string", ""),
        "new_string": ti.get("new_string", ""), "idx": idx, "line": line, "alt": alt,
        "at": datetime.datetime.now().timestamp(),
    }
    save_state(session_id, state)
    log(f"FIRED on {path}:{idx} {line.strip()!r} (budget left {left - 1})")
    respond("deny", puzzle(ti, idx, line))


if __name__ == "__main__":
    main()
