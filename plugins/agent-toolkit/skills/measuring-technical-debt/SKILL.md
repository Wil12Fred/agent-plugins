---
name: measuring-technical-debt
description: >
  Use when deciding whether a change is good enough to merge, when setting up a
  debt register, or when a debt signal has not moved in a long time. Covers the
  ratchet (a signal may fall, never rise), which quality characteristic each
  signal erodes, and the failure that keeps recurring — a signal structurally
  incapable of observing the thing it names. Do not use for judging a specific
  design; that is review, and a fake metric for it is worse than none.
---

# Measuring technical debt

A debt list maintained by hand becomes a wish list within a month. So every entry
is a **number the repository can produce on demand**, with a recorded baseline,
and one rule:

> **A signal may fall, never rise.** Adding to a debt fails the build; paying it
> down and re-recording the baseline is a normal commit.

What deliberately gets no signal: anything needing judgement — whether an
abstraction is right, whether a name is clear. Those belong in review. A fake
metric for them would be worse than none.

---

## Name the characteristic, not just the number

A rising number should say *what kind* of quality is being spent. Map each signal
to a quality characteristic (ISO/IEC 25010 is a serviceable vocabulary):

| Signal shape | Characteristic it erodes | Why it is debt |
|---|---|---|
| duplicated blocks | Maintainability | Two copies drift, and the difference surfaces in whichever is read least. |
| capability moved rather than ported | Functional suitability | It has no test and no guard. Growth means the migration is reversing. |
| tolerated broken links | Maintainability | Each is a document citing something that no longer exists. |
| skipped tests | Reliability | A green tick that proves nothing is worse than a missing one, because it looks like coverage. |
| open decisions | Security | Items needing a human — credential rotation, a grant. Debt precisely because no gate can close them. |
| untested commands | Functional suitability | Where the defects have been: a command that returned nothing for its whole life, indistinguishable from a correct answer. |
| code citations without a commit | Maintainability | A path and a line number are only true against one revision. |

---

## The failure that keeps recurring

**A signal that cannot observe the thing it names.**

Measured case: a signal called "unported scripts" counted rows in a manifest CSV.
A row existed only for files the migration already knew about, so scripts written
*after* the baseline could not raise the count. It sat flat for a fortnight while
exactly the debt it names accumulated — and its own docstring said *"growth means
the migration is being reversed"*.

The check to run on every signal you own:

1. **Can this number go up?** Construct the debt it names and confirm it rises.
   If you cannot make it rise, it is decoration.
2. **Does it measure the world, or a record of the world?** A count of rows in a
   file you maintain measures your bookkeeping.
3. **What is it blind to?** A gate scanning only one file extension misses the
   same debt in another — and being invisible is how a dead script survives for
   months.

That is failure mode 9 in `verifying-a-claim`, applied to metrics: **a gate that
has never fired is not proven, it is unmeasured.**

---

## Accepting a baseline is a decision

Re-recording a baseline at a *higher* number silently blesses a regression. When
a ratchet is red, the honest options are to pay it down or to say out loud, in
the commit, which regression is being accepted and why. "Re-accept so the build
goes green" is how a ratchet stops meaning anything.

Corollary for a **new** signal: introducing it with a large backlog turns the
build red from birth, and a gate that is red from birth gets skipped. Report
first, ratchet once the count reaches a floor you intend to hold.

---

## Before calling a change done

Name the characteristics it touched and the evidence for each. Not "tests pass" —
which test, asserting which rule, and what does it do when the rule is violated?
A test that cannot fail for the reason it claims is not evidence, and reverting
the change to watch it go red is the cheapest proof there is.
