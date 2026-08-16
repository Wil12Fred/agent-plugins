# agent-toolkit

Five skills and two subagents about **making an agent's claims checkable**. No
project knowledge, no company knowledge, no host names, no ticket keys.

| | |
|---|---|
| `verifying-a-claim` | fourteen ways a check passes while proving nothing, and the thirteen questions to answer before writing "verified", "blocked" or "ready" |
| `authoring-skills-and-subagents` | command vs skill vs subagent; writing a `description` that routes; turning "must not edit" into a guarantee; getting the thing loaded at all |
| `vetting-third-party-tooling` | never install a third-party MCP — clone, read, score 1-10, adapt or escalate |
| `verified-state-contract` | every claim about a live system carries the commit it was measured against, re-checked at the moment of use |
| `measuring-technical-debt` | the ratchet, which quality characteristic each signal erodes, and how a signal ends up unable to observe what it names |
| `@agent-repo-auditor` | audits a repository against the rules it wrote for itself. `permissionMode: plan` — reports, never edits |
| `@agent-research-analyst` | evaluates external tooling by cloning and reading it. Never installs, never executes a candidate |

## Install

**`SKILL.md` is a cross-vendor standard**, so the five skills run unmodified on
Claude Code, Codex CLI, Gemini CLI and Cursor. The *packaging* is what differs.

**Claude Code** — the plugin, which carries the skills *and* the two subagents:

```bash
claude plugin marketplace add https://<host>/<user>/agent-plugins.git
claude plugin install agent-toolkit@agent-plugins      # user scope: every project
```

**Codex CLI and Gemini CLI** — symlink the skills; both read `~/.agents/skills/`,
so one link covers both:

```bash
mkdir -p ~/.agents/skills
for s in skills/*/; do ln -sfn "$PWD/$s" ~/.agents/skills/"$(basename "$s")"; done
```

Codex also reads `~/.codex/skills/` if you prefer to keep them separate, and
accepts an optional `agents/openai.yaml` inside a skill folder for its own UI
metadata. None is required — the skills work without one.

**What does not port:** the two subagents. `permissionMode`, `tools` and
`isolation` are Claude Code frontmatter, and Codex has its own agent mechanism
with its own format. The skills are the portable half; the subagents are not.

## The MCP server

Installing the plugin also gives you `agentctl`, a read-only MCP server with two
tools. They exist because they were prose inside the auditor's prompt first: a
model asked "does this repository do spec-driven development?" answers
confidently either way, while a filesystem check answers with a path or with
nothing.

| Tool | Answers |
|---|---|
| `repo_detect` | which practices this repository has adopted, each with the paths that prove it |
| `repo_strays` | which executables sit outside the code directory, and which are declared exceptions rather than nobody's decision |

**Every tool is read-only because every command is.** There is no
`--allow-writes` to grant and no write path to guard — a stronger property than
a guarded write, and a cheaper one to verify.

Standalone, without the plugin:

```bash
uvx --from plugins/agent-toolkit agentctl detect .
uvx --from plugins/agent-toolkit agentctl strays .   # exits 7 when anything is undeclared
```

## Why it is a separate plugin

It was extracted from a repository whose other skills are all about one company's
product. A project that is not that company should not pay their context cost or
read their internals — so the split is by **how far a thing travels**.

Everything here is written to survive that extraction: no relative link leaves
this directory, and nothing inside names the company it came from. Moving it into
its own repository should be a `git mv` and a new marketplace entry.

## Where these came from

Every rule here is the residue of something that went wrong once — a green test
that measured zero, a gate wired to a signal nobody sends, a debt counter
structurally incapable of counting the debt it named. The examples in the skills
are generalised; the incidents that produced them stay in the repository this was
extracted from, which is the right place for evidence with ticket numbers on it.

That division is deliberate and worth keeping: **the skill is canonical for the
rule, the origin repository is canonical for what happened.**
