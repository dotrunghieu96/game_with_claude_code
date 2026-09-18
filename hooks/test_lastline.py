#!/usr/bin/env python3
"""Drive the hook the way Claude Code does: a JSON payload on stdin, JSON out.

Runs the real script in a subprocess so the env knobs are exercised for real. The picker
is never reached - every case here is decided by a gate before the model call.
"""
import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lastline.py")


BUDGET = tempfile.mktemp(suffix="-budget.json")


def run(payload, **env):
    # the hook's own knobs leak in from the session that runs the suite
    base = {k: v for k, v in os.environ.items() if not k.startswith("LASTLINE_")}
    e = dict(base, LASTLINE_BUDGET_FILE=BUDGET, **env)
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, env=e, timeout=60)
    return json.loads(r.stdout or "{}")


def edit(added, session="test", path="/tmp/x.py"):
    body = "\n".join(f"    x{i} = {i}" for i in range(added))
    return {"hook_event_name": "PreToolUse", "session_id": session,
            "tool_input": {"file_path": path, "old_string": "def f():\n    pass",
                           "new_string": f"def f():\n{body}"}}


def decision(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def reason(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    return cond


def main():
    ok = True
    counter = iter(range(1000))
    fresh = lambda: f"t{os.getpid()}-{next(counter)}"  # state files outlive the run

    ok &= check("off never fires",
                decision(run(edit(5, fresh()), LASTLINE_INTENSITY="off")) == "allow")

    ok &= check("single-line edit is too small",
                "too small" in reason(run(edit(1, fresh()), LASTLINE_INTENSITY="annoying")))

    # size deny: 60 added lines is under light's 80 and over heavy's 25
    ok &= check("light tolerates 60 added",
                decision(run(edit(60, fresh()), LASTLINE_INTENSITY="light",
                             LASTLINE_MAX_ADDED="80", LASTLINE_PICK_TIMEOUT="1")) == "allow")
    ok &= check("heavy denies 60 added",
                decision(run(edit(60, fresh()), LASTLINE_INTENSITY="heavy")) == "deny")
    ok &= check("deny says split it",
                "one concern each" in reason(run(edit(60, fresh()), LASTLINE_INTENSITY="heavy")))

    # the same oversized edit twice in a row must not loop
    sid = fresh()
    first = run(edit(60, sid), LASTLINE_INTENSITY="heavy", LASTLINE_COOLDOWN="0")
    second = run(edit(60, sid), LASTLINE_INTENSITY="heavy", LASTLINE_COOLDOWN="0")
    ok &= check("loop guard lets the second one through",
                decision(first) == "deny" and decision(second) == "allow")

    # hint levels reach the relay text
    import importlib.util
    spec = importlib.util.spec_from_file_location("ll", HOOK)
    for level, marker in [("veteran", "Relay the calls and their alternatives"),
                          ("normal", "what the surrounding lines expect"),
                          ("amateur", "in plain words what that line has to do")]:
        os.environ["LASTLINE_HINTS"] = level
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok &= check(f"{level} hints", marker in mod.hint_block("a why"))
    ok &= check("picker's why is relayed", "a why" in mod.hint_block("a why"))

    # --- the section render, straight off the module (no picker needed) ---
    body = "\n".join(f"    step{i}()" for i in range(40))
    ti = {"file_path": "/tmp/x.py", "old_string": "def f():\n    pass",
          "new_string": f"def f():\n{body}"}
    lines = ti["new_string"].splitlines()
    two = [{"idx": 2, "line": lines[2], "alt": "step1(retry=True)", "why": "no retry"},
           {"idx": 30, "line": lines[30], "alt": "step29(None)", "why": "drops the arg"}]
    out = mod.puzzle(ti, two, "an intent")
    ok &= check("both calls are marked", out.count("<- 1") == 1 and out.count("<- 2") == 1)
    ok &= check("untouched middle is collapsed", "unmarked lines ..." in out)
    ok &= check("collapsed block is not the whole file", "step20()" not in out)
    ok &= check("two calls number the moves", "why N" in out and "all 2" in out)
    one = mod.puzzle(ti, two[:1], "an intent")
    ok &= check("one call becomes a blank they type", "____" in one and "<- you" in one)
    ok &= check("the blank keeps before and after", "\nbefore\n" in one and "\nafter\n" in one)
    ok &= check("the blank has no moves to pick", "why N" not in one and "    ok " not in one)

    ok &= check("twins merge", len(mod.merge_twins([
        {"idx": 4, "line": "    a_ready = False", "alt": "x", "why": ""},
        {"idx": 5, "line": "    b_ready = False", "alt": "y", "why": ""}])) == 1)
    ok &= check("distant lookalikes survive", len(mod.merge_twins([
        {"idx": 4, "line": "    a_ready = False", "alt": "x", "why": ""},
        {"idx": 19, "line": "    b_ready = False", "alt": "y", "why": ""}])) == 2)

    # --- ok: the edit comes back untouched and the human gets the receipt ---
    sid = fresh()
    e = edit(5, sid)
    json.dump({"pending": {"file_path": e["tool_input"]["file_path"],
                           "old_string": e["tool_input"]["old_string"],
                           "new_string": e["tool_input"]["new_string"],
                           "calls": [{"idx": 2, "line": "    x1 = 1", "alt": "x1 = 2", "why": "w"},
                                     {"idx": 3, "line": "    x2 = 2", "alt": "x2 = 3", "why": "w"}],
                           "intent": "i", "at": __import__("datetime").datetime.now().timestamp()}},
              open(mod.state_path(sid), "w"))
    out = run(e, LASTLINE_INTENSITY="annoying")
    ok &= check("ok allows the edit", decision(out) == "allow")
    ok &= check("ok prints the receipt", "yours now" in out.get("systemMessage", ""))

    # --- a blank they passed on: same untouched edit, one call, different receipt ---
    sid = fresh()
    e = edit(5, sid)
    json.dump({"pending": {"file_path": e["tool_input"]["file_path"],
                           "old_string": e["tool_input"]["old_string"],
                           "new_string": e["tool_input"]["new_string"],
                           "calls": [{"idx": 2, "line": "    x1 = 1", "alt": "x1 = 2", "why": "w"}],
                           "intent": "i", "at": __import__("datetime").datetime.now().timestamp()}},
              open(mod.state_path(sid), "w"))
    out = run(e, LASTLINE_INTENSITY="annoying")
    ok &= check("passing on the blank still allows", decision(out) == "allow")
    ok &= check("passing says passed, not stood behind", "passed" in out.get("systemMessage", ""))

    print("\n" + ("all pass" if ok else "FAILURES"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
