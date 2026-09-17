# Lastline — design

Supersedes `idea.md`. That brief is kept as the record of the original thinking; where the
two disagree, this document wins.

Status: pre-v0, nothing built.

## The goal

Better code. The human reads what they send and ship. Make it fun where fun is possible.

In that order. Fun is the delivery mechanism, not the goal — which demotes the brief's
"fun before useful" from a gate to a preference. A thing that is dull but makes the code
better stays in; a thing that is fun and changes nothing does not.

Authorship is the mechanism, not the point: you cannot write the missing line without
reading the change.

**What this is not.** Three interruptions a day does not cover what you ship. Lastline
keeps the judgement alive so that when you do read, you see. Coverage is a different tool
and this one should not pretend to be it.

## What this overrules in the brief

| Brief | Now | Why |
|---|---|---|
| §6 v0.1 is guess-the-line | v0 is leave-me-a-line | §1 and §3 both say generation is the target; guessing trains spotting |
| §3 five seconds | no time limit | typing a line costs 15–30s and that is fine; frequency is what protects flow |
| §3 no LLM call in the hot path | model picks the line | the brief derived this from the clock, and the clock is gone |
| §6.2 fire with probability p≈0.2 | fire when a line qualifies | the picker is narrow enough to be its own rate limiter |
| §6.3 rank by incident-embarrassment | rank by writability | a line you can recognise is not a line you can type |
| §7 v0.2 later | v0.2 is v0 | — |
| §3 fun before useful | better code first, fun where possible | fun is how it gets used, not what it is for |

Unchanged and still binding: authorship over examination, never gate everything, one
screen no scrolling, and the §4 non-goals.

## 1. The shape

The agent is about to apply an edit. A `PreToolUse` hook cuts one line out of it, shows the
surrounding change, and you type the line yourself. Your line is what lands. Then the hook
shows what the agent would have written, beside yours.

You wrote the code. You also find out immediately whether you and the agent agreed.

## 2. The hook cuts the hole, not the agent

The model is the party being audited, so it cannot be asked to leave a gap — it will skip
exactly when the diff is big and it is mid-flow. The hook removes the line from
`new_string` itself and holds it in memory. No cooperation required, and nothing marked
`// ← you` ever exists on disk.

## 3. Your line ships; the agent's is shown as an alternative

The reveal is what keeps the jolt (§2 of the brief: prediction, reveal, verdict). Without
it the verdict is a red test minutes later, which is not a verdict.

The framing does the work here. The agent's line is **an** answer, not **the** answer. Three
words only — same, close, different — and *different* has to read as neutral. You are
allowed to be right and disagree.

Risk, accepted: this can quietly become grading against the agent. Watch for it.

## 4. Decision lines, not lookup lines

Only hand over a line whose content is a *judgement with few valid spellings*:

- the boundary in a comparison
- which branch catches the error
- the condition on a retry, backoff or timeout
- the order of two acquisitions

Never hand over a line that needs recall of an exact identifier, signature or argument
order. A line that is fun to guess at 80% confidence is infuriating to type at 100%.

This makes the picker a **filter**, not a ranker. No qualifying line in the diff, no
interruption. That is where "never gate everything" now comes from.

## 5. The agent is told, afterwards

The hook's stdout tells the agent that the line is the human's: treat it as intent, object
to it rather than edit it. Without this the agent reads the file on its next turn and
silently fixes you inside thirty seconds.

Cost, accepted: the audited party learns when it is being audited. Mitigated by telling it
only *after* the cut, per edit, never as a standing instruction — it cannot write bait for
a line it does not know will be taken.

## 6. A hard daily budget

N per day, start at 3. Spend on the first N qualifying lines, then silence until tomorrow.

This replaces the clock. Interruption cost is governed by how often, not how long — once
you are in it, take the time you need.

Known failure: the budget burns on the morning's trivia and the afternoon's good line
passes in silence. The log is the fix — record every qualifying line, including the ones
that arrived with an empty budget, and tune N and the bar from that.

## 7. The celebration

Tetris clears singles constantly with a blip and saves the flash for the four-line, which
is rare and which you set up on purpose. Lastline is graded the same way.

**Show the fork, not just the answer.** The picker has to name an `alternative` to prove a
decision existed, so the reveal shows it: yours, mine, and the other way it could have
gone. That is what makes a match mean anything - without it, `same` reads as "you agreed
with the agent" rather than "there were two real options and you took this one".

**In the moment, quiet.** `same` and `close` get a small mark. `different` gets the reveal
and nothing else. Deliberately underweighted — matching the agent means you predicted the
agent, and rewarding that loudly trains mimicry, which is the muscle this tool rejected.

**At session end, loud.** The flash is for a line of yours that **survived**: you wrote
something different from the agent, it shipped, and nothing changed it back before the
session ended. A `Stop` hook checks the file and says so by name — *your line at
retry.py:41 is still standing.*

Survival, not agreement. The agent folds to humans out of politeness, so its concession is
a worthless verdict; whether the line is still there is not.

This is the only thing here that makes the code measurably better — a divergence where you
were right is a bug that did not ship. Everything else is comprehension with a bow on it.

**Not a ledger.** Nothing crosses a session boundary. The hook holds `file:line → your
line` in a session-scoped temp file and the `Stop` hook deletes it. §4's cut of spaced
repetition and decaying scores stands; this is not the beginning of one.

## 8. Order of operations

Cheap local filters, then budget, then the model. Budget-last would mean fifty model calls
a day to spend three.

1. local: diff size, file type, generated/lockfile/format-only, cooldown
2. budget: any left today? no → allow, log, exit
3. model: name the decision line, or say there isn't one → no line means allow and log
4. cut, deny the edit, and hand the agent the puzzle to relay
5. on the re-applied edit: grade it, and send the reveal out as a `systemMessage`
6. tell the agent the line is the human's, log the outcome, remember it if they diverged
7. at `Stop`: for each remembered line, still there → flash; changed back → say nothing

Any failure at any step — no tty, model timeout, malformed response — allows the edit
silently. The hook never hangs a session and never blocks on a wrong line.

## 9. Still open

- What is actually on screen. One screen, no scrolling, but how much of the diff.
- The escape hatch. Enter skips — does skipping cost anything at all? (Current answer: no.)
- `Write` and `MultiEdit`. A new file has no surrounding change to read; a multi-edit has
  several. Probably `Edit` only in v0.
- Wording of the three verdicts, given *different* must not read as a mark.
- What the flash actually looks like in a terminal. It has to feel free, not earned.
- **The premise assumes decay, and LLM-first code has none.** The brief bet on a muscle
  that atrophied - a line you could derive but had not thought about. In code the agent
  wrote end to end you never had a model of the system, so the blank is an exam on
  material never covered, and wider context does not help because what is missing is not
  local. Observed 2026-09-17: the tool worked on `lastline.py`, the one file being read,
  and failed on a file that was only ever generated. Leading candidate if this bites
  again: ask for intent in plain words rather than syntax, and grade it against the two
  lines the picker already produces. Not built - the line-writing path is still being
  tried.

## 10. Spikes

1. **Can a `PreToolUse` hook prompt interactively?** Read `/dev/tty` while stdin carries the
   payload, in a real session. Everything still depends on this. Unchanged from the brief.
2. ~~**Does the prompt find decision lines?**~~ Answered yes on a hand-built set of three;
   still worth running over 50 real diffs to see the decline rate.
3. **How does the agent behave when told a line is the human's?** Does it actually object,
   or does it agree with whatever it is shown?
4. **Is survival detectable?** At `Stop`, can the hook tell "your line is still there" from
   "the file moved under it"? Line numbers drift. Match on content, not position.

## 11. Dogfood findings — 2026-09-17

Built a working hook and ran it against real edits in a live session. What that settled:

**Spike 1 is answered: no.** Claude Code runs hooks in their own session — `sid == pid`,
`tty_nr = 0`, `ENXIO` on `/dev/tty`. The only reachable terminal is the one the TUI owns:
writing to `/proc/$PPID/fd/0` lands and is visible, but reading splits keystrokes between
the hook and the TUI (`b'pe\x7ftpeita\xc6\xb0...'`, Enter never arrived) and corrupts the
display. Write works, read does not. Not a config problem.

**Input comes through the agent instead.** The hook denies the edit and instructs the
agent to ask; the human answers in the normal input box; the agent re-applies with their
line. Determinism survives because the hook blocks — the model is not choosing to ask.
The hook caches the original `new_string` and checks the re-applied one differs at exactly
the blanked line, so tampering is detectable.

**Deny reasons reach the agent; allow reasons do not.** The reveal therefore goes out as a
top-level `systemMessage`, which the TUI prints to the human directly — which is better
than the relay, because it takes the verdict out of the agent's hands.

**Leaking the answer is fine.** The agent already wrote the line, and Claude Code's own
input box completes from context, so the answer leaks whatever we do. Decided: that is
acceptable. Retyping the line forces you to read it, which is the goal; whether you could
have derived it unaided is secondary. This retires the anti-hint machinery and most of
§3's worry about controlling the verdict.

**A bare blank needs context, not shape.** `____` with four lines either side is
unanswerable — is it a return, what type, what language. Decided: keep the blank bare and
widen the context (full before/after, call sites of the enclosing function) rather than
leave a stub. Tension to watch: this collides with "one screen, no scrolling".

**Pending state dies with the session.** An in-flight puzzle is keyed on `session_id`, so
when the session identity changes underneath you the answer has nowhere to land and a
fresh puzzle fires instead. Seen once, 2026-09-17. Not fixed.

**The agent must state the intent.** One sentence, change-level, above the block. Without
it the human is reading a diff of code they have never seen.

**The model picker works; the regex one never did.** Spike 2 answered. `claude -p` rides
the existing Claude Code auth - no API key, no SDK, nothing to set up - and costs 9-16s,
which is fine behind the budget and cooldown gates (at most three calls a day). The prompt
makes the model supply an `alternative` line as proof a fork exists, and rejects the
alternative unless the two behave *observably* differently: `attempt <= max_retries` is a
whole extra retry and counts, `elapsed <= COOLDOWN` is a microsecond and does not. With
that, it declines a pure-lookup diff, declines the boring cooldown line it had been
picking, and finds the real fork in an error-handling change. The regex picker is gone -
it scored comparisons, not decisions, and on model failure the hook now just allows.

Code lives in `hooks/lastline.py`, registered in `.claude/settings.json`. Working: picker,
daily budget, fire/resolve round trip, three verdicts with off-by-one forced to
*different*, decision log. Not built: the model picker, the cooldown, the `Stop`-hook
survival flash.
