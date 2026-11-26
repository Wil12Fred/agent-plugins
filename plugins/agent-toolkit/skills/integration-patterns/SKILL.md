---
name: integration-patterns
description: >
  Use when parallel branches or parallel agents keep colliding, or when a gate
  pushes you toward a bad fix: a size guard that counts comments, a JSON or
  generated file that conflicts on every merge, a function silently redefined by
  a merge, a guard green locally and red after commit, generated content that is
  grammatical but absurd, hints that leak the answer, a server still running old
  code after a merge, a device connection that keeps dropping. Maps each symptom
  to the established pattern, how to apply it, what not to do, and its source.
  Do not use for judging whether a metric is worth having (measuring-technical-debt)
  or whether a check proved anything (verifying-a-claim).
---

# Integration patterns

Every problem below was hit in one session of a learning-app monorepo where up
to seven agents worked on parallel branches and one integrator merged them. None
was new. Each has a pattern somebody already wrote down; the cost was in
re-deriving it under pressure, and twice in nearly taking the wrong fix first.

The rule this skill exists to enforce: **when a gate fails, find the pattern
before you find a workaround.** A workaround that makes the gate green without
addressing what it measures is the failure mode `verifying-a-claim` describes,
committed on purpose.

| Symptom | Pattern | Section |
|---|---|---|
| A line-count ceiling fails because code was documented | Measure code, not text; budget the artifact | [1](#1-a-size-budget-that-counts-comments) |
| One JSON/YAML bank conflicts on every merge | One record per file, or a structured merge driver | [2](#2-every-branch-appends-to-one-big-file) |
| A merge leaves two `def f` in a module; the second wins silently | Redefinition lint (F811) as a gate | [3](#3-a-name-defined-twice-in-one-module) |
| A guard passes locally, fails after commit | Gate the content that will be committed | [4](#4-a-guard-that-cannot-see-the-file-you-just-added) |
| Generated docs conflict on every merge | Generate, don't merge; `--check` in the gate | [5](#5-generated-artifacts-that-conflict) |
| Frames × slot lists produce grammatical nonsense | Typed slots with selectional restrictions; exhaustive validation | [6](#6-template-generated-content-that-is-absurd) |
| A hint or distractor reveals the answer | Item-writing rules as tests, over every instance | [7](#7-a-hint-that-gives-the-answer-away) |
| A long-running server answers with yesterday's code | The service reports its commit; deploy waits for it | [8](#8-a-server-still-running-old-code) |
| A device link drops; the fix needs a weaker security mode | Record the trade-off; the human runs the step | [9](#9-a-fix-that-needs-a-weaker-security-mode) |
| Parallel branches cost more to merge than to write | Short-lived branches, file ownership, integrate early | [10](#10-parallel-branches-and-the-cost-of-integrating-late) |

---

## 1. A size budget that counts comments

**Symptom.** A guard caps a client at N physical lines. Two branches each add a
feature *with explanatory comments*; together they cross N by a few lines. The
cheapest green is to delete comments.

**Pattern.** Measure the thing the budget is about. A line ceiling exists to
keep *code* small; comments and blank lines are not code. Count SLOC (non-blank,
non-comment lines) the way `cloc`/`tokei`/`scc` do, or — if the concern is what
ships — budget the **built artifact in bytes** (minified, gzipped), which strips
comments by construction: webpack `performance.maxAssetSize`, Size Limit,
Lighthouse budgets.

**How.**
1. Re-measure the current code with the new metric and record both numbers.
2. Set the new ceiling deliberately, with a margin you can justify — not the old
   number in new units, which silently loosens the budget by the comment ratio.
3. Write the decision where the original budget was argued (ADR or the guard's
   docstring): old metric, new metric, why, who decided, the date.

**Do not.** Delete comments to fit. Raise the number without saying so. Both
make the metric the target, which is Goodhart's law in its usual form: a measure
that becomes a target stops measuring.

**Sources.** cloc — https://github.com/AlDanial/cloc ·
webpack performance budgets — https://webpack.js.org/configuration/performance/ ·
Size Limit — https://github.com/ai/size-limit ·
Goodhart's law (Strathern 1997 wording) — https://en.wikipedia.org/wiki/Goodhart%27s_law

## 2. Every branch appends to one big file

**Symptom.** Content lives in one large JSON array. Every branch adds items, so
every merge conflicts, and resolving by hand risks dropping or duplicating
records.

**Pattern.** Make concurrent additions touch different files.
- **One record per file** (or per small shard), assembled at build time. The
  same idea as changelog fragments: towncrier/blurb exist precisely because one
  shared changelog conflicted on every pull request.
- If the file must stay single, a **merge driver**: `.gitattributes`
  `bank.json merge=jsonrecords` plus a `merge.jsonrecords.driver` that does a
  three-way merge **per record id** (ours/theirs/base), failing on a real
  same-record conflict. The built-in `merge=union` is only safe for line-oriented
  files whose lines are independent — never for JSON.

**How (until the data is sharded).** Resolve item by item against the merge
base: an item changed on one side takes that side; changed on both sides is a
real conflict and stops the merge. Then re-run every derived step (curriculum
sync, generated docs) instead of hand-merging their outputs.

**Do not.** Pick "ours" or "theirs" for the whole file. Hand-edit conflict
markers inside a 50 000-line JSON.

**Sources.** gitattributes, merge drivers — https://git-scm.com/docs/gitattributes ·
towncrier news fragments — https://towncrier.readthedocs.io/

## 3. A name defined twice in one module

**Symptom.** Two branches each add `_validate_structure` to the same module.
Git merges cleanly; Python keeps the *later* definition; dozens of tests fail
far from the cause.

**Pattern.** Redefinition is a lint error, not a review finding: pyflakes/ruff
**F811** (`redefined-while-unused`) flags a name rebound before use. Make it part
of the gate that runs after every merge, not an opt-in. Fowler's *semantic
conflict* is the general name: a textual merge that succeeds and a program that
no longer does what either side meant.

**How.** Enable F811 (and the rest of the `F` family) in the gate. When it
fires after a merge, rename by *what each function checks*, not by branch.

**Do not.** Delete one definition because "it was added second".

**Sources.** Ruff F811 — https://docs.astral.sh/ruff/rules/redefined-while-unused/ ·
Semantic conflict — https://martinfowler.com/bliki/SemanticConflict.html

## 4. A guard that cannot see the file you just added

**Symptom.** A repository guard scans `git ls-files`. A new, not-yet-tracked
file passes `make check` locally; after `git add` the same guard fails.

**Pattern.** Gate the content that will be committed. Either include untracked
files (`git ls-files --cached --others --exclude-standard`), or run the gate on
staged content (pre-commit stashes unstaged changes and checks the index), or on
a clean checkout in CI. The rule to carry: a gate result is only about the tree
it actually read.

**How.** Before reporting "green" on a change that adds files, `git add` them
first, or run the guard with untracked files included.

**Sources.** git ls-files — https://git-scm.com/docs/git-ls-files ·
pre-commit (staged files only) — https://pre-commit.com/

## 5. Generated artifacts that conflict

**Symptom.** Reference docs generated from the code are committed. Every branch
regenerates them, so every merge conflicts in files nobody wrote.

**Pattern.** Never resolve a generated file by hand: **regenerate it** after the
merge and let a `--check` mode in the gate prove it matches. Mark such files
`linguist-generated` (hidden from review diffs) and, if they must be committed,
give them a merge strategy that takes either side because the next step
overwrites it anyway. Better still: generate at build time and do not commit.

**Sources.** GitHub, marking generated files —
https://docs.github.com/en/repositories/working-with-files/managing-files/customizing-how-changed-files-appear-on-github ·
gitattributes — https://git-scm.com/docs/gitattributes

## 6. Template-generated content that is absurd

**Symptom.** Exercises built by crossing sentence frames with context lists are
all grammatical and some are nonsense ("at university, our teacher used to make
the bed"). A sample review passed; a full audit found about one in five wrong.

**Pattern.** Slots are typed. Each value declares the contexts it is compatible
with — the NLG name is **selectional restrictions** — so the generator can only
combine compatible values (bind the time marker to the action, not to the
frame). Then validate **exhaustively**: expand every combination at validation
time and run deny-patterns (`forbid`) over all of them. Sampling is for
estimating a defect rate, not for shipping.

**Do not.** Ship a cross product reviewed by sample. Fix one absurd instance
without fixing the slot that produced all its siblings.

**Sources.** Template-based generation, slots and constraints (van Deemter,
Krahmer, Theune, *Computational Linguistics* 2005) — https://aclanthology.org/J05-1002/ ·
selectional restrictions in lexical choice — https://arxiv.org/abs/cs/0008023

## 7. A hint that gives the answer away

**Symptom.** A hint shown *before* a retry contains the correct option ("Is
she…? / She isn't…" for "Ana ___ ready").

**Pattern.** Classic item-writing rules: no cue to the keyed answer in the stem,
the options or the feedback; distractors plausible and wrong for a content
reason. Turn the rule into a test over **every** item and every template
instance: normalise text, and fail if any hint or pre-answer formula contains an
accepted answer as a word sequence. Keep a control case so the test is proven to
fire.

**Sources.** Haladyna, Downing & Rodriguez (2002), multiple-choice item-writing
guidelines, doi:10.1207/s15324818ame1503_5 —
https://experts.umn.edu/en/publications/a-review-of-multiple-choice-item-writing-guidelines-for-classroom/

## 8. A server still running old code

**Symptom.** A long-running local API was started before several merges. The
client calls a new route; the server answers "no route".

**Pattern.** A process must say what it is running. Expose the **commit hash**
(or build id) on the health endpoint and have whatever restarts or deploys it
wait until the reported hash equals the one just built. A 200 from `/health`
alone can come from the old process. Restart on every merge that touches the
service; a supervisor or file watcher makes that automatic.

**Sources.** Verified-state discipline for live systems — this plugin's
`verified-state-contract` skill · build identity on the version/health endpoint,
a common practice summarised at https://dev.to/velprove/api-health-check-patterns-what-healthz-should-return-ln8

## 9. A fix that needs a weaker security mode

**Symptom.** Wireless device debugging drops when the phone locks. The usual fix
(`adb tcpip` on a fixed port) was refused by the agent's permission system.

**Pattern.** The refusal is the system working. Do not route around it with
another tool. State the trade-off (fixed-port TCP debugging is unauthenticated
transport; pairing-based wireless debugging is the secure mode and is tied to a
trusted network), give the human the exact command to run themselves if they
accept it, and offer the non-security alternative (keep the screen awake while
charging).

**Sources.** Android, run apps on a device (wireless debugging, `adb tcpip`
caveats) — https://developer.android.com/studio/run/device

## 10. Parallel branches and the cost of integrating late

**Symptom.** Seven agents on seven branches; each finished quickly; integrating
them took longer than writing them, and two merges had to be aborted and handed
back.

**Pattern.** Integration cost grows with time apart and with shared files.
- **Short-lived branches, integrated at least daily** (trunk-based development);
  merge the smallest finished branch first.
- **File ownership per branch.** Before fanning out, list which files each
  branch may change; two branches that both need one file are one branch, or
  one of them waits.
- **A merge queue** (or a single integrator) that runs the full gate on the
  merged result, because each branch's own green proved nothing about the
  combination.
- When a merge breaks a gate the *other* branch introduced, hand it back to the
  branch that owns the content rather than patching it in the merge commit.

**Sources.** Fowler, Continuous Integration — https://martinfowler.com/articles/continuousIntegration.html ·
Patterns for managing source code branches — https://martinfowler.com/articles/branching-patterns.html ·
DORA, trunk-based development — https://dora.dev/capabilities/trunk-based-development/

---

## Before reaching for a workaround

1. Name the symptom in one line and find it in the table. If it is not there,
   search for the established pattern before improvising — it almost always
   exists.
2. Apply the pattern, not the shortest edit that turns the gate green.
3. If the pattern changes a gate (a metric, a ceiling, a merge strategy), record
   the decision where the gate is argued — old rule, new rule, why, who, when.
4. Prove the fix with a check that fails without it (`verifying-a-claim`).
