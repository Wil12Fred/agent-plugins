# Claude Code Instructions

@AGENTS.md

## What this repository is

Project-agnostic tooling for coding agents: five Python packages under `apps/`,
two Claude Code plugins under `plugins/`, and a VS Code extension under `tools/`.

**The one rule that governs everything: nothing here may name a company, a
product, a hostname, a ticket key or a person's account** — in code, docstrings,
test fixtures or default values. `AGENTS.md` explains why and what follows.

## The skills in this repository apply to work in it

They are not only shipped from here, they are the standard held here:

- `verifying-a-claim` — before writing "verified", "blocked" or "ready"
- `authoring-skills-and-subagents` — before adding either
- `vetting-third-party-tooling` — before adding any dependency you did not write
- `verified-state-contract` — before documenting how a live system behaves
- `measuring-technical-debt` — before adding a gate, and when one stops moving

## Auditing this repository

Use its own tools. That is the point of them, and reasoning out an answer a
command already gives is the thing `measuring-technical-debt` criticises:

```bash
agentctl detect .     # which practices this repo has adopted
agentctl strays .     # executables outside the code directory
make check            # lint + typecheck + tests + plugin manifests
```

`@agent-repo-auditor` runs the whole audit and reports; it never edits.

## Before committing

`make check` must be green. It is six packages and takes seconds; there is no
excuse shaped like "it was only a docs change", because the documented counts in
the READMEs are checked by nothing and drift silently — if you change what a
package contains, change what its README says it contains.
