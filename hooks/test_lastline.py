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
    e = dict(os.environ, LASTLINE_BUDGET_FILE=BUDGET, **env)
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
    for level, marker in [("veteran", "That is all they get unasked"),
                          ("normal", "which types are in play"),
                          ("amateur", "what someone running the code would actually see")]:
        os.environ["LASTLINE_HINTS"] = level
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok &= check(f"{level} hints", marker in mod.hint_block("a why"))
    ok &= check("picker's why is relayed", "a why" in mod.hint_block("a why"))

    print("\n" + ("all pass" if ok else "FAILURES"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
