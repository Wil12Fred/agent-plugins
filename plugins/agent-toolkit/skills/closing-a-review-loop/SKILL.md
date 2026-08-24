---
name: closing-a-review-loop
description: >
  Use when work must not be called finished until reviewers have signed off —
  running iterative review rounds, deciding when a loop has converged, or
  building the thing that stops an agent declaring victory early. Also use when a
  round keeps finding defects in the fixes for the previous round, and when a
  convergence gate you wrote needs to be trusted by somebody else. Do not use for
  deciding what a reviewer should look at — that is the review itself — or for
  judging whether one piece of evidence is sound, which is `verifying-a-claim`.
---

# Closing a review loop

An agent that decides when it is finished will decide it is finished. Not from
dishonesty — from the same place every defect in this file comes from: the
question it asked itself was adjacent to the question that mattered.

This is what to build instead, and what breaks when you build it.

It is the longest skill here, and the reason is the subject: this is a loop with
more failure modes than steps. The enumerations *are* the skill — six ways a
gate fails closed, four mechanisms of which three fail identically — and each
entry is one incident that was believed and was wrong. A summary of them would
be the thing they warn against.

## The mechanism: only a hook cannot be forgotten

Four options, and three of them fail the same way.

| approach | who decides to stop | failure |
|---|---|---|
| Ask an orchestrator subagent "am I done?" | the agent, by remembering to ask | forgetting to ask is identical to deciding to stop |
| An interval loop that self-paces | the agent, by ending the loop | same |
| Instructions in memory or a prompt | the agent | same |
| **A `Stop` hook** | **the harness** | it runs whether the agent wants it to or not |

The first three move the problem. Only the hook removes it, because a Stop hook
is executed by the harness after the agent finishes responding and can refuse
the stop. Memory and preferences cannot do this: they are read by the thing
being constrained.

**Forget-proof, not tamper-proof.** An agent with write access to the settings
file can delete the hook, and in a loop like this it usually has exactly that
access. What the hook removes is *forgetting to check* — the failure that
actually happens. It does not remove *deciding not to*, and a skill that claims
otherwise is making the same mistake as a gate that trusts its own state file.

Two constraints worth knowing before designing around it. A `Stop` hook must be
a **command** hook — the `prompt` and `agent` hook types exist only for tool
events. And the harness itself is reported to override a Stop hook after some
number of consecutive blocks, silently, which matters for the give-up branch
below. **Find out what that number is on your harness and version rather than
taking one from a document** — the figure quoted to the author of this skill
came from a reviewer without a source, and this file is not a second-hand source
for it either.

## What "still open" means, and what it does not

The obvious rule — *block while anything is pending* — is wrong, and wastes
turns proving it. A dispatched reviewer that is still running re-invokes the
session when it finishes. Holding the turn open to wait for it accomplishes
nothing and burns tokens doing it.

The gate blocks on work being **neglected**, not on work being **in flight**:

| state | block? |
|---|---|
| unresolved findings | yes — there is something to fix now |
| pending, not dispatched | yes — there is something to dispatch now |
| dispatched and running | no — the report will wake the session |
| dispatched, clean, nothing pending | no — converged |

This distinction is not obvious from reading the design. It appeared on the
first round where there was genuinely nothing to do but wait, and the gate fired
twice for nothing.

## Six ways a gate fails closed while its header says it fails open

A convergence gate must never be able to strand a session. The failure mode has
to be "the loop stopped early", never "the session cannot end" — otherwise the
gate is worse than nothing, and nobody can safely commit it for other people.

Every one of these was written by an author who believed the property held, and
found by fuzzing rather than by reading:

1. **A wrong-typed number.** `consecutive_blocks: "many"` is well-formed JSON,
   so it passes a `jq -e .` validity check, and then `string + number` fails
   silently inside the counter update. The counter freezes, the give-up branch
   never advances, and the gate blocks forever.
2. **An empty field, when validation concatenates.** `case "${a}${b}${c}" in
   *[!0-9]*)` looks equivalent to checking each value and is not: an empty value
   contributes nothing to the joined string, so `""` reads as valid. Validate
   per field; `case "" in ''` matches, concatenation hides it.
3. **An unwritable temp directory.** `tmp=$(mktemp)` returning empty does not
   trip `set -u` — the variable is set, merely empty — and without `set -e` the
   `&&` chain short-circuits and execution falls through to the block. If the
   gate cannot record that it blocked, it must not block: `|| exit 0`.
4. **A give-up branch above the harness cap.** If the harness overrides at N and
   your limit is above N, yours is decoration: the harness ends the turn first,
   silently, and the explanatory message never prints. Set yours below N — and
   establish N by observation, not by trusting a number in prose. This is
   exactly where the author of this skill was handed an unsourced figure.
5. **Ignoring `stop_hook_active`.** The harness sets this to say *you are already
   why this turn is continuing*. A hook that never reads stdin cannot honour it,
   and a bad condition sustains itself.
6. **Silently inert.** A gate that disarms itself on convergence stays disarmed
   until something re-arms it. A state file listing open work while the gate is
   off reads as guarded and guards nothing. Detect that combination and say so —
   it is the one failure a silent pass cannot report.

Fuzz all six before claiming the property. "Fails open" is a claim like any
other, and the same discipline applies to it as to a test result.

## The hole you cannot close from inside

A gate reads state. If the agent writes that state, the gate is
self-certification: it catches *the agent forgot to record the work* and is
blind to *the agent recorded work it never did*.

That is not hypothetical. A state file once listed four reviewers as dispatched
that had never been dispatched; the gate blocked for the right reason on
invented data, and had no way to know.

Close what you can with a source the agent cannot forge. The Stop payload
carries the harness's own account of what is running — **and this exact field is
the one the anecdote below is about.** One reviewer asserted it existed, then
retracted it as imaginary; a second observed it in a real capture; the capture
was right. Confirm it in your own capture before relying on it:

```
background_tasks: [{"id": "...", "type": "subagent", "status": "running", ...}]
```

Prefer it over the claim, in **both** directions — a claim of reviewers that are
not running should not buy a pass, and running reviewers should earn one even if
the state file forgot to say so. Say which of the two the answer came from.

What stays open is the finding count. Whether a defect is genuinely resolved is
not observable by a hook; only a second party reading the report can close it.
Write that limit down rather than letting the gate imply more than it delivers.

## Verify the reviewer, not only the work

A reviewer's report is evidence, not a verdict. Three things happened in one
sequence of rounds:

- A reviewer asserted a payload field existed, with a schema. Nothing had been
  observed, so the advice was not acted on. **Declining to build on a described
  schema is what made the next two bullets survivable.**
- The same reviewer's retraction was *also* wrong — the field did exist. A
  second reviewer observed it in a real capture. **The capture settled it, not
  either reviewer.**
- **The loop owner** reported a mutant killing 12 tests; the reviewer re-ran it
  and got 3, because the two mutants were not the same mutant under one label.
  The verdict held; the number did not. Written this way round because the first
  draft of this file had it backwards, filing a loop-owner error as evidence
  about reviewers — under a heading about verifying reviewers. Reconcile a
  figure that does not reproduce **in whichever direction it came from**,
  starting with your own.

So: instrument before building, treat a described schema as a hypothesis, and
reconcile numbers that do not reproduce. A reviewer correcting themselves is a
good sign about the reviewer and tells you nothing about which version is true.

## When a round is done, and when the loop is

A round is done when every dispatched reviewer has reported and every finding is
resolved or explicitly recorded as debt.

The loop is done when a round produces **zero defects** — not zero comments.
Separate the two in every report, and make reviewers do the same:

- a **defect** is something factually wrong, or a guard that does not guard
- a **suggestion** is something that could be better

If suggestions count, the loop never terminates, because a reviewer asked to
review will always find one. If nothing counts, it terminates immediately and
means nothing.

Expect several rounds. Each round's fixes are new surface for the next, and in
practice the defects get smaller and more self-referential as it converges —
a rule broken by the sentence introducing it, a comment describing the fix
rather than the code. That shape is the signal you are close. A round that finds
nothing but is also reviewing nothing new is not convergence; it is an empty
round.

## Repetition is not a measurement of flakiness

A flaky test is a claim, and it gets the same treatment as any other: reproduce
it before you accept it, and reproduce it before you declare it fixed. The trap
is that the obvious experiment — run it many times, count the reds — measures
the wrong thing whenever **the condition that makes it flaky is the condition
repetition removes**.

One case, and it cost four rounds — the fourth because the first diagnosis was
wrong in an instructive way. A control test hammered an unlocked connection
until a race appeared, and failed when it saw none. Two reviewers reported 40/40
and 60/60 clean; the loop owner reported three consecutive clean gates and
called it validated. All three were sound measurements of nothing.

Changing the *load* found it: eight suites at once went 2 of 8 red. So the
lesson was written down as "contention serialises the interleaving, therefore
repetition on a quiet machine cannot see it" — and that was **half right, which
is worse than wrong, because it closed the question.**

A later audit ran the same test at rest under a *different invocation* — the
module on its own, rather than the whole suite — and it went red there too, on
an idle machine. Three runs of the ordinary developer command would have found
it. The condition was never really load; load was one thing that happened to
serialise the schedule, and the invocation was another, cheaper one that nobody
varied because the first explanation already fit.

**A flake rate is a property of the invocation as much as of the machine, and
repeating one invocation measures only that invocation.** Before quoting a green
streak: name the condition the failure needs, say whether your runs had it, and
list what you did *not* vary. The first plausible mechanism is the one that
stops people looking.

One more caution from the same episode: those two audits measured that
middle condition six-fold apart (3 of 30 against 36 of 60). Two observers
disagreeing by that much about a rate is normal for races and is not a reason to
discard either; it is a reason not to quote a single figure for one.

The generalisation is worth more than the case. **When a test can fail for a
reason that is not about the code, it does not belong in the gate.** A useful
question for finding those: *what would this test do if the defect it guards
were reintroduced?* The control above passed 8 times out of 8 with the shipped
lock deleted — it never touched the shipped code at all — and its own docstring
said so plainly, one round before anyone read it as grounds for deletion.

## Two things that keep happening

**A guard inherits the blind spot it was built for.** A test written to catch
"new structure, undocumented" descended into the one directory its author was
looking at, and could not see the second of the three instances it was written
to prevent. When you build a guard for a recurring defect, check it against
*every* past instance, not the one in front of you.

**Fixing a fail-closed bug can leave one shape of it behind**, which is worse
than not claiming the property, because now the claim is documented. Entry 2
above is exactly that: the fix for entry 1, applied in a form that missed the
empty case.

**A test can vary every dimension its author thought of and still miss the one
the users occupy.** A suite guarding an answer-safety property was widened three
times: from one course to all of them, from right-answers-only to right and
wrong, and from one item to the whole run. Each widening was found by a reviewer
and each was real. It still produced only *homogeneous* histories — all-right or
all-wrong — and a leak conditioned on having seen **both** a hit and a miss
survived every test in the repository, shipping the answer key to the mixed
history every actual learner produces after two questions. The next round found
a fifth: every test answered with the canonical uppercase label a helper
returns, while the shipped program accepts lowercase, padding and full option
text, so a leak keyed on answer *form* was invisible too.

The pattern is not "test more dimensions". It is that the dimensions an author
enumerates are the ones they already know exist, and the gap is always where the
real user stands. Ask what a user does that no fixture does — and expect the
answer to take several rounds and more than one reviewer to surface. It took
four here.
