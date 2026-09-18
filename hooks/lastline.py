#!/usr/bin/env python3
"""Lastline — the hook cuts one line out of an edit and makes the human write it.

Hooks are setsid'd with no controlling tty, so input cannot come from here (spike 1).
Instead the hook denies the edit and instructs the agent to relay: ask the human, then
re-apply with their line. The hook keeps the original and grades what comes back.
"""
import datetime
import difflib
import hashlib
import json
import os
import re
import sys
import subprocess
import tempfile

VERSION = "0.1.0"  # x-release-please-version

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "lastline.log")
BUDGET_FILE = os.environ.get("LASTLINE_BUDGET_FILE") or os.path.join(HERE, ".budget.json")
PENDING_TTL = 600  # a puzzle the agent never resolved is dead after 10 min
MODEL = os.environ.get("LASTLINE_MODEL", "claude-opus-5")
PICK_TIMEOUT = int(os.environ.get("LASTLINE_PICK_TIMEOUT", "45"))

# How often, not how hard: the picker's bar is the same at every intensity.
# A tighter setting buys itself more forks to pick from by capping the edit smaller.
INTENSITY = {
    "off":      {"budget": 0,    "cooldown": 0,     "max_added": 0},
    "light":    {"budget": 1,    "cooldown": 14400, "max_added": 80},
    "normal":   {"budget": 3,    "cooldown": 3600,  "max_added": 40},
    "heavy":    {"budget": 8,    "cooldown": 120,   "max_added": 25},
    "annoying": {"budget": 9999, "cooldown": 0,     "max_added": 25},
}
LEVEL = INTENSITY.get(os.environ.get("LASTLINE_INTENSITY", "normal").lower(), INTENSITY["normal"])
DAILY_BUDGET = int(os.environ.get("LASTLINE_BUDGET", LEVEL["budget"]))
COOLDOWN = int(os.environ.get("LASTLINE_COOLDOWN", LEVEL["cooldown"]))
MAX_ADDED = int(os.environ.get("LASTLINE_MAX_ADDED", LEVEL["max_added"]))
HINTS = os.environ.get("LASTLINE_HINTS", "normal").lower()

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
    log(f"{decision} v{VERSION}: {reason.splitlines()[0][:120]}")
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


PICK_PROMPT = """You are marking the judgment calls in a code change, for a human about to accept it.

A judgment call is a line where a competent engineer could reasonably have written
something DIFFERENT, and the difference would matter. Not "is this line important" -
"was there a real fork here". A boundary that could defensibly go either way, a branch
that could handle the error differently, an ordering that could be reversed.

Not a judgment call:
- there is only one sensible way to write it
- an import, a log, a comment, boilerplate, or fixed by the line above it
- a different spelling of the same behaviour

For each call you must supply `alternative`, a different line a good engineer might
plausibly have written instead - AND the two must behave observably differently:
  `attempt <= max_retries` vs `<` - counts, that is a whole extra retry
  `elapsed <= COOLDOWN` vs `<`  - does not count, that is a microsecond

Mark AT MOST 3, fewest that are real. Everything you do not mark is being declared
mechanical, so do not pad. Also give `intent`: one sentence, what this change is for.

Reply with JSON only, no prose:
{"intent": "<one sentence>", "calls": [{"line": <0-based index into AFTER>,
 "why": "<12 words max, what breaks if the alternative ships>",
 "alternative": "<the other line>"}]}
or {"calls": []}

FILE: %s

BEFORE:
%s

AFTER (indexed):
%s
"""


def merge_twins(calls):
    """Neighbours that read alike are one decision the picker counted twice."""
    keep = []
    for c in calls:
        twin = next((k for k in keep if abs(k["idx"] - c["idx"]) <= 2 and
                     difflib.SequenceMatcher(None, norm(k["line"]), norm(c["line"])).ratio() >= 0.6), None)
        if twin:
            log(f"merged call {c['idx']} into {twin['idx']}")
        else:
            keep.append(c)
    return keep


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
    calls = []
    for c in (data.get("calls") or [])[:3]:
        i = c.get("line")
        if isinstance(i, int) and i in eligible and (c.get("alternative") or "").strip():
            calls.append({"idx": i, "line": lines[i], "alt": c["alternative"],
                          "why": c.get("why") or ""})
    calls = merge_twins(calls)
    if not calls:
        log("picker declined (no calls)")
        return None
    log(f"picker marked {[c['idx'] for c in calls]}")
    return calls, (data.get("intent") or "")


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


HINT_BLOCKS = {
    "veteran": """Relay the calls and their alternatives as written.{fork} Nothing more
unasked.""",
    "normal": """With each call, add what the surrounding lines expect of it and which
types are in play.{fork} Name the competing options, do not pick one.""",
    "amateur": """With each call, say in plain words what that line has to do - what it
receives, what the lines after it need from it - and for each alternative, what someone
running the code would actually see differently.{fork} Name them all, do not pick one.""",
}


def hint_block(why=""):
    fork = f" The fork the picker saw: {why}." if why else ""
    return HINT_BLOCKS.get(HINTS, HINT_BLOCKS["normal"]).format(fork=fork)


def puzzle(ti, calls, intent=""):
    """One fork is a line worth typing; several are a section worth reading."""
    return blank(ti, calls[0], intent) if len(calls) == 1 else marks(ti, calls, intent)


def blank(ti, call, intent=""):
    """Before/after with the one line cut out, because a hole with no intent is unwritable."""
    path = ti.get("file_path", "?")
    lang, cmt = LANGS.get(os.path.splitext(path)[1], ("", "#"))
    lines = ti["new_string"].splitlines()
    idx = call["idx"]
    after = []
    for i, text in enumerate(lines[:60]):
        indent = re.match(r"\s*", text).group()
        after.append(f"{indent}____________________  {cmt} <- you" if i == idx else text)
    before = ti.get("old_string", "").splitlines()[:60] or ["(new file)"]
    name = enclosing_name(lines, idx)
    sites = call_sites(name, path) if name else []
    used = ("\n\nwhere `{}` is used\n```\n{}\n```".format(name, "\n".join(sites))
            if sites else "")
    return f"""LASTLINE is holding this edit. Do not retry it as-is.

Ask the human to write one line of it themselves. Show them this and nothing else:

`{path}` - {intent}

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
they render highlighted. {hint_block(call.get("why", ""))}

Rules:
- Do not write the line for them. Hint as much as they ask for - typing it is the point, not
  guessing it. A blank they cannot approach is a failure of the hint, not a win.
- Ask once, then stop and wait for their answer.
- When they answer, re-apply this exact Edit with their line in place of the blank,
  changing nothing else.
- If they say skip or decline, re-apply this exact Edit unchanged.
"""


def marks(ti, calls, intent=""):
    """The whole section, with the forks marked. You accept by naming one."""
    path = ti.get("file_path", "?")
    lang, cmt = LANGS.get(os.path.splitext(path)[1], ("", "#"))
    lines = ti["new_string"].splitlines()
    marked = {c["idx"]: n for n, c in enumerate(calls, 1)}
    # one screen or it does not get read: +-4 lines around each mark, rest collapsed
    keep = {j for i in marked for j in range(i - 4, i + 5)}
    body, run = [], 0
    for i, text in enumerate(lines):
        if i in keep:
            if run > 2:
                body.append(f"  {cmt} ... {run} unmarked lines ...")
                run = 0
            elif run:
                body.extend(f"  {t}" for t in lines[i - run:i])
                run = 0
            body.append(f"! {text}   {cmt} <- {marked[i]}" if i in marked else f"  {text}")
        else:
            run += 1
    if run > 2:
        body.append(f"  {cmt} ... {run} unmarked lines ...")
    name = enclosing_name(lines, calls[0]["idx"])
    sites = call_sites(name, path) if name else []
    forks = "\n".join(
        f"{n}. `{c['line'].strip()}`\n   vs `{c['alt'].strip()}` - {c['why']}"
        for n, c in enumerate(calls, 1))
    # "not referenced anywhere" is noise for a handler; only real sites earn the space
    used = ("\n\nwhere `{}` is used\n```\n{}\n```".format(name, "\n".join(sites))
            if sites else "")
    n = len(calls)
    hints = hint_block()
    return f"""LASTLINE is holding this edit. Do not retry it as-is.

Show the human this and nothing else, fences included, then stop and wait:

`{path}` - {intent}

{n} judgment calls. Everything unmarked is mechanical.

```diff
{chr(10).join(body)}
```

{forks}{used}

    ok      you have read all {n} and stand behind them - the edit applies as written
    why N   make me defend one before you decide
    fix N   write one yourself instead of mine

Rules:
- Relay the block exactly, `diff` tag included, so it renders highlighted. {hints}
- `why`: answer it straight - what the alternative would cost, whether theirs is better.
  If they are right, say so. Then ask again. Do not re-apply the edit yet.
- `ok`: re-apply this exact Edit unchanged.
- `fix`: re-apply this exact Edit with their line replacing the call they named, nothing else changed.
- Do not write a line for them, and do not talk them out of `why`.
"""


def resolve(pending, ti, session_id, state):
    """They answered. Either they stood behind it, or they rewrote one call."""
    calls = pending["calls"]
    a = pending["new_string"].splitlines()
    b = ti["new_string"].splitlines()
    changed = ([i for i in range(len(a)) if a[i] != b[i]] if len(a) == len(b) else None)
    marked = {c["idx"]: c for c in calls}

    if changed is None or any(i not in marked for i in (changed or [])) or len(changed or []) > 1:
        state["pending"]["at"] = datetime.datetime.now().timestamp()
        save_state(session_id, state)
        respond("deny", "That is not the edit I held. Re-apply the original, changing at "
                        "most one marked line. Nothing else.")

    state.pop("pending", None)
    if not changed:                                   # ok, or a blank they passed on
        save_state(session_id, state)
        log(f"resolved ok calls={[c['idx'] for c in calls]}")
        rows = "\n".join(f"    {n}  {norm(c['line'])}" for n, c in enumerate(calls, 1))
        if len(calls) == 1:
            return ("Allowed unchanged - they passed on writing it. Do not raise it again.",
                    "  lastline - passed\n" + rows)
        return ("Allowed as written. They have read it and stood behind it - do not "
                "re-explain the calls.",
                "  lastline - yours now\n" + rows)

    idx = changed[0]                                  # fix N
    c = marked[idx]
    theirs, ours = b[idx], c["line"]
    v = verdict(theirs, ours)
    state.setdefault("standing", []).append(
        {"file": ti["file_path"], "line": norm(theirs), "idx": idx})
    save_state(session_id, state)
    log(f"resolved fix {idx} {v} theirs={norm(theirs)!r} ours={norm(ours)!r}")
    mark = {"same": "\n    \u2022 same", "close": "\n    \u2022 close", "different": ""}[v]
    rows = [f"    yours   {norm(theirs)}", f"    mine    {norm(ours)}"]
    alt = norm(c.get("alt", ""))
    if alt and alt not in (norm(theirs), norm(ours)):
        rows.append(f"    or      {alt}")
    return ("""Allowed. Their line is in, and they have been shown both - do not repeat them.

Their line is what shipped. It is intent, not a draft: if you think it is wrong, say so as
an objection and let them decide. Do not quietly edit it back.""",
            "  lastline\n" + "\n".join(rows) + mark)


def on_stop(payload):
    """Session over. A line of theirs still standing is the only loud moment."""
    session_id = payload.get("session_id", "")
    state = load_state(session_id)
    still = [e for e in state.get("standing", []) if survived(e)]
    # Stop fires at the end of every turn, and asking them costs a turn - so an open
    # puzzle has to outlive it, or it can never be answered (dogfood, 2026-09-17).
    pending = state.get("pending")
    if pending and datetime.datetime.now().timestamp() - pending["at"] < PENDING_TTL:
        save_state(session_id, {"pending": pending})
    else:
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

    if len(cands) > MAX_ADDED:
        # hash() is salted per process and the hook is a new process each edit
        key = path + ":" + hashlib.sha1(ti.get("new_string", "").encode()).hexdigest()
        if state.get("denied_size") == key:
            state.pop("denied_size", None)   # asked once; a second deny is a loop
            save_state(session_id, state)
            allow(f"too big ({len(cands)} added), already asked")
        state["denied_size"] = key
        save_state(session_id, state)
        respond("deny", f"""{len(cands)} lines at once is more than anyone reads.

Split this into edits of one concern each - the smallest chunk that stands on its own and
leaves the file working - and apply them one at a time. Do not re-send this edit whole.""")
    state.pop("denied_size", None)

    chosen = pick_model(ti, cands)
    if chosen is None:
        allow("no decision line")

    calls, intent = chosen
    spend()
    state["pending"] = {
        "file_path": path, "old_string": ti.get("old_string", ""),
        "new_string": ti.get("new_string", ""), "calls": calls, "intent": intent,
        "at": datetime.datetime.now().timestamp(),
    }
    save_state(session_id, state)
    log(f"FIRED on {path} calls={[c['idx'] for c in calls]} (budget left {left - 1})")
    respond("deny", puzzle(ti, calls, intent))


if __name__ == "__main__":
    main()
