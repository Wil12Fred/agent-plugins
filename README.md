# agent-plugins

A plugin marketplace for coding agents. Everything here is **project-agnostic**:
no company, no product, no host names, no ticket numbers.

| Plugin | What it is |
|---|---|
| [`agent-toolkit`](plugins/agent-toolkit) | Five skills and two subagents about making an agent's claims checkable |

## Install

**Claude Code** — the full plugin, skills and subagents:

```bash
claude plugin marketplace add git@github.com:Wil12Fred/agent-plugins.git
claude plugin install agent-toolkit@agent-plugins     # user scope: every project
```

**Codex CLI / Gemini CLI** — the skills only. `SKILL.md` is a cross-vendor
standard, so they run unmodified; `~/.agents/skills/` is read by both, so one set
of links covers them:

```bash
git clone git@github.com:Wil12Fred/agent-plugins.git
cd agent-plugins && mkdir -p ~/.agents/skills
for s in plugins/agent-toolkit/skills/*/; do
  ln -sfn "$PWD/$s" ~/.agents/skills/"$(basename "$s")"
done
```

The two subagents do not port: `permissionMode`, `tools` and `isolation` are
Claude Code frontmatter, and other agents have their own mechanisms.

## What is in `agent-toolkit`

| | |
|---|---|
| `verifying-a-claim` | fourteen ways a check passes while proving nothing, and the questions to answer before writing "verified", "blocked" or "ready" |
| `authoring-skills-and-subagents` | command vs skill vs subagent; writing a `description` that routes; declaring guarantees instead of requesting them |
| `vetting-third-party-tooling` | never install a third-party MCP — clone, read, score 1-10, adapt or escalate |
| `verified-state-contract` | every claim about a live system carries the commit it was measured against |
| `measuring-technical-debt` | the ratchet, and how a debt signal ends up unable to observe what it names |
| `@agent-repo-auditor` | audits a repository against the rules **it** wrote for itself — after detecting which of those rules it actually adopted |
| `@agent-research-analyst` | evaluates external tooling by cloning and reading it; never installs, never executes a candidate |

## The idea behind all of it

Every rule here is the residue of something that went wrong once: a green test
that measured zero, a gate wired to a signal nobody sends, a debt counter
structurally incapable of counting the debt it named.

Two principles run through them:

**A passing check proves the check passed.** Whether it proves your claim is a
separate question, and it is the one nobody asks when the result is the one they
were hoping for.

**A rule the project never adopted is not a finding.** The auditor detects what a
repository has actually taken on before judging it, and reports "did not apply"
separately from "could not measure" — because collapsing those makes a clean repo
look unexamined and an unexamined one look clean.

## Licence

MIT.
