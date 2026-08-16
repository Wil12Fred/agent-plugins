---
name: authoring-skills-and-subagents
description: >
  Use when writing or reviewing a Claude Code skill, subagent or plugin — deciding
  whether new capability should be a command, a skill or a subagent, writing a
  `description` that actually routes, turning "the agent must not edit" from a
  request into a guarantee, and getting the thing loaded at all. Also use when a
  skill exists but never seems to be offered. Do not use for the content of the
  skill itself, only its shape and its wiring.
---

# Authoring skills and subagents

Two failures dominate, and neither is about writing.

**The thing is never loaded.** A skill can be perfectly well-formed, listed in
your documentation, and read by nobody. This failure is *silent*: a malformed
file fails validation, a missing doc entry fails a test, and an uninstalled skill
produces no signal at all — the agent simply never offers it, and nobody can
notice an option that was never presented.

**The `description` does not route.** It is what the agent reads when deciding
whether this applies, and often *all* it reads. Written as a summary, it loses to
whichever neighbour happens to sound closer.

---

## Command, skill, or subagent?

Get this wrong and the same capability exists three times.

| The capability is… | It belongs in |
|---|---|
| deterministic — same answer every run | **a CLI command**: testable, CI-callable, and an MCP tool for free |
| knowledge an agent needs *while* doing its own work | **a skill** |
| work needing judgement, its own context, or a tool restriction | **a subagent** |

A well-typed command is automatically a well-described tool; a `dict[str, Any]`
parameter is an unusable one.

---

## The `description` is the router

Three clauses, and the third is the one people omit:

- **when to use it** — the situation, in the words someone would actually use;
- **what it does** — concretely enough to distinguish it from its neighbours;
- **when *not* to use it**, naming where to go instead.

That last clause is what stops a domain skill being pulled into a forensics
question. Once you have more than a handful of skills, they overlap, and "Do not
use for…" is the only thing that resolves it.

---

## Where things load from, and which wins

**Skills.** The agent reads its own directory (`~/.claude/skills/`), not your
repository's. Connecting them is a symlink or a plugin — an extra step that gets
skipped, silently. Prefer shipping them in a plugin: no symlink, no per-machine
setup, and a fresh clone works.

**Subagents** need no install step at all: `.claude/agents/` is discovered by
walking up from the working directory, so a committed file is live for anyone who
clones. Precedence, highest first: managed settings, `--agents` flag, the
project's `.claude/agents/`, your `~/.claude/agents/`, a plugin's `agents/`.

Restart if the directory did not exist when the session started.

---

## Declare guarantees; do not request them

A sentence in a system prompt is a request. These are not:

| Frontmatter field | Value | What it makes impossible |
|---|---|---|
| `permissionMode` | `plan` | Editing. "Reports and never edits" in prose stops nothing. |
| `tools` / `disallowedTools` | allowlist / denylist | Reaching a tool the role has no business using. |
| `isolation` | `worktree` | Two agents fighting over one checkout. |
| `maxTurns` | a number | A loop that never terminates. |

**One caveat measured in practice: `permissionMode: plan` does not stop a
subagent writing its own `memory`.** If you set `memory`, say in the prompt that
memory holds *decisions*, never *measurements* — remembered state is state nobody
re-measured, which is the failure an auditing agent exists to catch.

---

## Plugins: the packaging

A plugin bundles `skills/`, `agents/`, `commands/`, `hooks/`, `.mcp.json`,
`bin/` under one directory with `.claude-plugin/plugin.json`. Install once, use
from any directory. Two things worth knowing before you build one:

- **Installing copies the directory** into a cache, unless the marketplace entry
  uses a `command` source with `"mode": "link"`, which uses it in place. Copy is
  wrong for a large repository — it is stale the same day.
- **A link-mode plugin does not load inside its own directory.** That is usually
  what you want: inside the repo, the project's own files load unnamespaced;
  outside, the plugin supplies them under `plugin-name:`.
- **Your top-level context file cannot ship in a plugin.** The validator says so
  outright. Project instructions stay project-scoped, by design.

---

## The checklist

1. Write the file, with the `description` as a router including its "do not use
   for" clause.
2. Declare the guarantees: `permissionMode`, narrowed `tools`, `isolation`.
3. **Prove it loads.** Ask a session, from a directory where it should apply, to
   list what it can invoke. Do not infer this from the file existing — that is
   failure mode 9 in `verifying-a-claim`: a gate that has never fired.
4. Add a routing rule wherever your other skills are routed, so there is a
   written answer to "which one owns this".
