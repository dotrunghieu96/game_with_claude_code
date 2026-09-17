# Lastline

A Claude Code hook that cuts one line out of an edit and makes you write it.

The agent is about to apply a change. A `PreToolUse` hook picks one line where a competent
engineer could reasonably have written something else, blanks it, and blocks the edit. You
type the line. Yours is what ships, and you are shown what the agent would have written.

Not a quiz — you cannot write the missing line without reading the change, which is the
point. At most three a day by default, with a cooldown, and it never blocks on a wrong
answer.

## Setup

Needs Python 3 and the `claude` CLI on `PATH` — the picker runs `claude -p` on your
existing Claude Code auth, so there is no API key to set.

Copy the hook into your project and register it:

```bash
mkdir -p hooks .claude
curl -o hooks/lastline.py https://raw.githubusercontent.com/dotrunghieu96/game_with_claude_code/master/hooks/lastline.py
chmod +x hooks/lastline.py
```

Swap `master` for a tag from [Releases](../../releases) to pin a version.

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit", "hooks": [
        { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/lastline.py\"", "timeout": 300 }
      ]}
    ],
    "Stop": [
      { "hooks": [
        { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/lastline.py\"", "timeout": 30 }
      ]}
    ]
  }
}
```

That goes in `.claude/settings.json`. The 300s `PreToolUse` timeout covers the picker call
(9–16s) plus the round trip while you think. Add `hooks/lastline.log` and
`hooks/.budget.json` to `.gitignore`.

Start a new session and edit something. Nothing visible means no line qualified, which is
the normal case — `hooks/lastline.log` records every decision either way.

## Intensity

How often it fires. The picker's bar does not move — a tighter setting buys itself more
forks to choose from by capping how much the agent may change at once.

| `LASTLINE_INTENSITY` | per day | cooldown | max lines per edit |
|---|---|---|---|
| `off` | 0 | — | — |
| `light` | 1 | 4h | 80 |
| `normal` (default) | 3 | 1h | 40 |
| `heavy` | 8 | 2m | 25 |
| `annoying` | every edit | none | 25 |

An edit adding more than the cap is denied with "split this into edits of one concern
each". Sending the same one twice gets it through — the hook asks once, it does not loop.

## Hints

How much the agent tells you before you type. Independent of intensity: pick by what you
know about the code, not by how often you want interrupting.

| `LASTLINE_HINTS` | you get |
|---|---|
| `veteran` | one sentence of intent, the before/after, the call sites |
| `normal` (default) | + types in play, what the surrounding lines expect, the options named |
| `amateur` | + what the line must do in plain words, and what each option changes at runtime |

At every level the agent will hint further if you ask, and at none of them will it write
the line for you.

Raw overrides, if the presets do not fit: `LASTLINE_BUDGET`, `LASTLINE_COOLDOWN` (seconds),
`LASTLINE_MAX_ADDED`, `LASTLINE_MODEL`, `LASTLINE_PICK_TIMEOUT`.

## Releasing

Commit subjects are [Conventional Commits](https://www.conventionalcommits.org) —
`feat:` bumps the minor, `fix:` the patch, `feat!:` or a `BREAKING CHANGE:` footer the
major. Anything else (`docs:`, `chore:`, `refactor:`) ships without a release. Bodies are
still prose; only the subject line has to conform.

On push to `master`, release-please opens or updates a release PR that accrues the
changelog. Merging it tags, cuts the GitHub release, and bumps `VERSION` in the hook —
which the log prints beside every decision, so a log line says which version made it.

`CHANGELOG.md`, `version.txt` and `VERSION` belong to release-please. Do not hand-edit them.

## Layout

- `design.md` — the current design, and what it overrules
- `idea.md` — the original brief, kept as the record of the first thinking
- `hooks/lastline.py` — the hook
- `hooks/test_lastline.py` — drives it with crafted payloads; `python3 hooks/test_lastline.py`
- `.claude/settings.json` — how it is registered

Pre-v0. Working: the picker, intensity and hint levels, the fire/resolve round trip, the
verdict, the survival flash.
