---
name: vetting-third-party-tooling
description: >
  Use when considering an external MCP server, agent collection, prompt pack or
  orchestration technique — before installing anything. Clone it, read it, score
  it 1-10 on five axes, then either adopt the idea as your own or write it up as
  a decision for a human. Also use when asked "what tooling are we missing".
  Do not use for ordinary libraries, which are a normal dependency decision.
---

# Vetting third-party tooling

An MCP server runs as a child process of the agent session, **with that session's
environment**. If your session can reach production data, so can anything you
install. So "add this MCP server" is not a convenience decision.

The standing rule:

> **Never install a third-party MCP server, agent or prompt pack. Clone it, read
> it, understand it, score it. Then either adapt it as yours, or write it up as a
> decision for a human.**

---

## Ask the cheap question first

**Do we already have this?** The platform ships a lot — recurring prompts,
built-in exploration and planning subagents, subagent memory, worktree isolation.
Your own CLI may already cover it. Two of the first three candidates examined in
practice were already covered, and the check costs a minute.

---

## The procedure

1. **Clone into a throwaway area.** Never next to your real work, never as a
   dependency of your workspace.
2. **Read it end to end.** Not the README — the code. What it executes, which
   environment variables it reads, where it sends data, its transitive
   dependencies.
3. **Score it.** Rubric below.
4. **Route on the score**:

| Score | What happens | Who decides |
|---|---|---|
| **7–10** | Worth installing as-is. Written up as a decision record — what it is, the score per axis, what it can reach, the version to pin. **Still not installed.** | a human |
| **≤ 6** | Adapt it. Build your own skill / subagent / command, informed by their design. | the agent |

5. **Attribute what you adapted**, in the file itself: *"Adapted from `<url>`,
   read at commit `<sha>`, `<date>`. Scored N/10."* Without that line the
   provenance is gone in one refactor.

---

## The rubric

Five axes, 0–2 each. **A zero on blast radius or auditability is a rejection**,
whatever the total.

| Axis | 0 | 1 | 2 |
|---|---|---|---|
| **Maintenance & provenance** | anonymous, stale, no licence | active, one maintainer | organisation-backed, releases, permissive licence |
| **Blast radius** | reads broad env / arbitrary exec / phones home | scoped env, one known host | no secrets, no network |
| **Auditability** | unreadable in an afternoon, huge dep tree | readable, several deps | small, few deps, runnable tests |
| **Fit** | covers a fraction | covers most, needs glue | covers it, the way you would have built it |
| **Replaceability** | months to rebuild | a week | a day or two — and then it is yours |

**Replaceability scoring high pushes toward adapting, not installing.** A thing
you can write in two days is a thing you should own, because then it carries your
guarantees.

### Hard vetoes

- Unpinned execution (`npx <pkg>@latest`, `uvx` with no version). An upstream
  compromise becomes yours at the next session start.
- Anything reading your whole environment rather than named variables.
- Telemetry you cannot switch off.
- Code you could not finish reading.

### Never execute the candidate

Reading foreign code is safe; running it is not — its first act may be to read
the environment you are running in. No `npx`, no test suite of theirs. If a claim
can only be settled by running it, that is a finding for the human, not a step
for you.

---

## Libraries are a different question

A library that parses a PDF or transcodes a video is a normal dependency: install
it, pin it, use it behind your own command.

The distinction is **who is in control of the loop**. A library is called by your
code. An MCP server *is* an agent-facing surface with its own process and its own
prompt-injection surface. Install the first, never the second.

The shape for new capability: install the library → wrap it in a command with a
test → it becomes an MCP tool automatically through your own server.

---

## Techniques are free

Some candidates are patterns rather than software — orchestration loops,
context-compaction strategies, research-agent designs. Nothing to install and
nothing to trust: take the idea, write it as yours, attribute it. **Prefer this
outcome.** A pattern you implement carries your guarantees; a package you install
carries somebody else's.
