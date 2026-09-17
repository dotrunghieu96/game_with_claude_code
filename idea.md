# Lastline — build brief

A Claude Code plugin that makes you write one line of every diff, and guess one line
before you see it. Comprehension by authorship, not by quiz.

Status: pre-v0. Nothing is built. The first task is a paper test, not code.

---

## 1. The problem

Agents produce plausible code faster than a human can build judgment about it.
"Review" degrades into "approve." Two things rot: the ability to *spot* wrong code,
and the ability to *generate* right code. This tool targets the second more than the
first, because generation is the muscle that actually atrophied.

Trunk-based development, so there is no PR quarantine stage. Any intervention has to
happen at write time, before the edit lands in the working tree. That is a hard
constraint, not a preference.

## 2. Essential experience

The feeling being designed for is **"I still have it."** The small jolt of knowing the
answer a half-second before it is revealed.

That jolt has a precise anatomy:

1. a micro-prediction the user commits to
2. an immediate reveal
3. a verdict the user does not control

Remove any of the three and the feeling does not fire. This is why "explain this change
in your own words" fails — there is no crisp moment of being right.

## 3. Design constraints (these are cuts, not aspirations)

**Five seconds.** The common-path interaction is one hidden line, one guess, one reveal.
One screen, no scrolling. If it takes twenty seconds it is dead.

**No LLM call in the hot path.** Falls directly out of the five-second rule. The blank is
chosen by a local heuristic, synchronously, in milliseconds. Anything needing generation
happens asynchronously for a *later* interaction, never the current one.

**Fun before useful.** If one hidden line is not enjoyable with zero score, zero streak
and zero consequence, no amount of scoring rescues it. Test it naked.

**Authorship over examination.** The reward is the code itself: you filled the blank, the
diff applied, your fingerprints are on it. Not a badge.

**Never gate everything.** Gating every edit is the fastest route to uninstalling this by
Friday. Most diffs get nothing at all.

## 4. Non-goals for v0

Explicitly do not build these. They were considered and cut, each by a specific
constraint above. Do not reintroduce them without revisiting the constraint.

- XP, points, levels, badges — extrinsic reward crowds out the intrinsic jolt
- Team leaderboards — wrong essential experience entirely
- Comprehension coverage as a CI gate — punishes the org, not the moment
- Trust-as-currency / variable agent autonomy — good idea, too big for v0
- Sabotage: mutated code written to the working tree — unacceptable under trunk-based
- Spaced repetition / decaying scores — needs a persistence layer that does not exist yet
- MCP server — see §7, deliberately deferred

## 5. Step zero: the paper test

Run this before writing any code. It is falsifiable in a day.

Three times tomorrow, on a real diff Claude produced: cover one line you expect to be
interesting, guess it out loud, then look.

Log exactly two things per trial: did you guess right, and did you feel anything.

Read the result like this:
- Guessed right and felt the jolt → build it
- Guessed right and felt nothing → the line was too obvious; the blank-picker is the
  whole product and needs work
- Could not guess at all → the line was arbitrary; same conclusion, opposite direction
- Did not want to do the third trial → there is no toy here, stop

The sweet spot is a line you *could* derive from surrounding context but had not
consciously thought about. That gap is exactly the comprehension at risk.

## 6. v0.1 — guess-the-line

The smallest thing with a real reveal.

**Flow.** Claude is about to apply an edit. A `PreToolUse` hook inspects the diff. If it
qualifies (§6.2), the hook hides one line, prints the surrounding context to the
terminal, and waits for a guess. Enter skips. On submit it reveals the real line, marks
match / near / different, and allows the edit. It never blocks on a wrong answer — being
wrong is information, not a gate.

**Why a hook and not a tool.** The model is the party being audited. An MCP tool is
something the model chooses to call, and it will skip it exactly when the diff is big and
it is mid-flow. Hooks fire regardless. That determinism is the product.

### 6.1 Mechanics

- `PreToolUse` matching `Edit|Write|MultiEdit`
- Hook receives the tool input as JSON on **stdin** — so the interactive prompt must read
  from `/dev/tty`, not stdin. This is the main technical unknown; spike it first (§8).
- Returns `hookSpecificOutput.permissionDecision: "allow"` on stdout, exit 0
- Timeout guard: if no `/dev/tty` is available (CI, piped, non-interactive), allow
  silently and do nothing. Never hang a session.

### 6.2 When to fire

Start deliberately dumb and tune from logs:

- skip if the diff is under ~5 changed lines
- skip generated files, lockfiles, formatting-only changes
- skip if a check already fired in the last N minutes (rate limit the interruption)
- otherwise fire with probability p, starting around 0.2

Log every decision — fired or skipped — plus the file, diff size, and outcome. The log is
how the heuristic gets tuned in v0.2, and it is more valuable than the feature itself.

### 6.3 Picking the line (the actual product)

Everything rests on this. A bad blank is boring or impossible. Cheap local heuristics,
ranked by how embarrassing the line would be to get wrong in an incident:

1. comparison operators and boundary conditions (`<` vs `<=`, off-by-one)
2. error-handling branches — the part everyone skims
3. the condition in a retry, backoff, or timeout
4. a cache key, lock acquisition, or anything order-dependent
5. fallback: the line with the highest count of identifiers already seen in context
   (derivable, therefore guessable)

Never hide: imports, boilerplate, log statements, comments, a line whose content is
determined by the line above it.

### 6.4 Grading the guess

Exact match is too strict; whitespace and variable-name variation should count. Normalize
aggressively — strip whitespace, compare token sequences. Three verdicts only: match,
near, different. Do not score anything. Do not persist anything in v0.1.

## 7. Later (do not build yet)

- **v0.2 — leave-me-a-line.** The authorship variant. The agent deliberately leaves one
  line of the change unwritten with a `// ← you` marker, and you type it. Failure mode is
  a compile error or red test, never a silent logic bug. Suspected to be stronger than
  guess-the-line because the code does not exist until you write it.
- **v0.3 — shadow diff.** Two versions shown, one mutated, pick the real one. The mutant
  lives only in the hook's memory and never touches the working tree. Needs async mutant
  generation (`PostToolUse` generates the next puzzle) because mutation quality requires
  an LLM call and the hot path forbids one. Mutate *intent* — a plausible-but-wrong retry
  policy, a lock in the wrong order, a cache key missing a field — not operators.
- **v0.4 — `/kata`.** Pull a diff from last week's git log into a scratch worktree,
  mutate it, make it a puzzle. Completely decoupled from the write path, zero prod risk.
- **MCP server as the brain.** Once there is state worth keeping — the ledger, puzzle
  generation, kata history — move it behind an MCP server and keep the hook as a thin
  reflex. Benefit: the same server works in other agents, only the hook layer stays
  Claude-specific. Not before there is state worth keeping.
- **Session receipt.** A `Stop` hook printing an honest line: N edits, M checked, K
  guessed. A mirror, not a punishment.

## 8. Spikes to run first

1. **Can a `PreToolUse` hook prompt interactively?** Read from `/dev/tty` while stdin
   carries the payload, inside a real Claude Code session. Everything depends on this. If
   it does not work, the fallback design is `PostToolUse` + a deferred puzzle, which is a
   materially different and weaker product.
2. **How long does the round trip actually take?** Measure against the five-second budget
   with a stopwatch, not intuition.
3. **Does the blank-picker pick interesting lines?** Run it offline over the last 50 diffs
   in the repo and eyeball the choices. No hook needed for this.

## 9. Open questions

- Should a check ever block on a wrong guess, or always allow? (Current answer: always
  allow. Revisit only if always-allow turns out to be ignorable.)
- Is the right trigger a path glob, a diff-size threshold, or a semantic "this touches
  invariants" signal from the agent itself?
- Does guess-the-line stay interesting after two weeks, or does it need the difficulty to
  track skill? Flow says it will need it; do not build it until boredom is observed.
- What is the honest escape hatch for 6pm during an outage — and does using it cost
  anything at all?
