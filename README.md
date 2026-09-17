# Lastline

A Claude Code hook that cuts one line out of an edit and makes you write it.

The agent is about to apply a change. A `PreToolUse` hook picks one line where a competent
engineer could reasonably have written something else, blanks it, and blocks the edit. You
type the line. Yours is what ships, and you are shown what the agent would have written.

Not a quiz — you cannot write the missing line without reading the change, which is the
point. At most three a day, with a cooldown, and it never blocks on a wrong answer.

- `design.md` — the current design, and what it overrules
- `idea.md` — the original brief, kept as the record of the first thinking
- `hooks/lastline.py` — the hook
- `.claude/settings.json` — how it is registered

Pre-v0. Working: the picker, the budget, the fire/resolve round trip, the verdict.
Not built: the session-end survival flash.
