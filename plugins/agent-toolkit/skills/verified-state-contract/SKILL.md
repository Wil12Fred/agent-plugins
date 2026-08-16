---
name: verified-state-contract
description: >
  Use when documentation describes how a live system behaves — a queue, an
  endpoint, a deployment — and a reader will take it as present tense. Requires
  every such claim to carry the commit it was measured against and the date it
  was measured, in a machine-readable block, re-checked at the moment of use
  rather than on a schedule. Also use when a document "feels stale" and you need
  a way to tell. Do not use for claims about code that ships with the document.
---

# The verified-state contract

A document that says "the queue works like this" is making a claim about a moving
target. Systems deploy on merge, so the sentence was true against one commit and
nothing records which. Six months later an agent reads it as present tense and is
confidently wrong.

One idea fixes it: **every claim about system state carries the commit it was
measured against and the date it was measured.** Not the date the file was
edited — those are different, and the difference is the whole point.

---

## The block

One `## Verified state` section per document, containing one fenced `yaml` block.
Machine-readable on purpose: prose cannot be parsed, so prose cannot be checked.

````markdown
## Verified state

```yaml
verified_at: 2026-08-16          # the day this was MEASURED, not edited
repos:
  - repo: some-service
    prod: { ref: main, commit: "690dae0d", date: 2026-08-14 }
    dev:  { ref: dev,  commit: "f3bd7098", date: 2026-08-16 }
    in_dev_not_prod: [TICKET-991]     # the difference between the two rows
tooling:
  cli:      ["your-cli thing list"]
  packages: ["your-package"]
  e2e:      ["path/to/e2e/"]
debt:
  - ticket: TICKET-972
    what: the gate's e2e is red on purpose — the code shipped, the behaviour did not
    since: 2026-08-11
    commit: "665f002f"
```
````

`in_dev_not_prod` and `debt` may be empty lists, and **an empty list is an
answer**: "measured, nothing found". A *missing* key says "never measured". Keep
those distinguishable.

---

## Re-verify at the moment of use

Audits happen on demand, so "we will re-check quarterly" does not survive contact.
Move the check to where it cannot be skipped:

> **A document carrying this block is re-checked as the first step of using it.**
> If it is stale, re-measure and update it *before* answering the question that
> was asked.

Stale means any of: `verified_at` older than your ceiling (two weeks is a sane
default); a recorded commit is no longer the ref's head; a `debt` item's commit is
now in the default branch; an `in_dev_not_prod` entry has shipped.

---

## Four ways a block looks measured and is not

**A stale clone measures your disk, not production.** Reading a branch head
locally reports whatever you last fetched — and if the remote moved host, a fetch
can *succeed* and still return a frozen answer. Record where `origin` points, and
report a claim resolved against the wrong remote as **unverified**, not measured.

**`--is-ancestor` lies about a squashed merge.** Work squashed on the way in
leaves the change present and the commit absent, so a paid debt looks open. Match
on the ticket identifier as well, and believe the diff when they disagree.

**A commit id without a date is unfalsifiable.** A bare sha cannot be compared to
anything by a reader; with a date beside it, staleness is arithmetic.

**Quote the commit.** A short sha is hex, and about 4% are all digits — which
YAML loads as an *integer*, dropping any leading zero. `0001234` becomes `1234`,
matches nothing, and is reported as drift in a system that never drifted.

---

## And: deployed is not working

The `prod` row says a commit is deployed, not that the behaviour is live. Code
can ship with its own e2e red on purpose. That is why `debt` is a separate list
and not a footnote on the `prod` row.
