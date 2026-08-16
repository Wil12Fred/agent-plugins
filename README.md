# agent-plugins

Tooling for coding agents: a **CLI**, an **MCP server**, five **skills** and two
**subagents**. Everything here is project-agnostic — no company, no product, no
host names, no ticket numbers.

```
plugins/agent-toolkit/     a Claude Code plugin
├── src/agentctl/          the CLI and the MCP server
├── skills/                5 skills — SKILL.md, so Claude Code, Codex, Gemini and Cursor all read them
├── agents/                2 subagents — Claude Code only
└── tests/                 39 tests

apps/slack-bridge/         a service, not a plugin
├── src/slackbridge/       drive Claude Code and Codex sessions from a Slack thread
└── tests/                 55 tests
```

---

## The CLI

```bash
uvx --from plugins/agent-toolkit agentctl --help
```

| Command | Answers |
|---|---|
| `agentctl detect [path]` | which practices this repository has adopted, each with the paths that prove it |
| `agentctl strays [path]` | which executables sit outside the code directory, and which are declared exceptions rather than nobody's decision. Exits 7 when anything is undeclared |
| `agentctl clipboard copy` | put text on the clipboard **and verify it landed** |
| `agentctl mcp` | serve the above as MCP tools over stdio |

Every command takes `--json` and answers with exactly one envelope, so a script
gets structured output and a meaningful exit code from the same binary a person
reads.

**Nothing here writes.** There is no `--force`, no `--apply`, and no
`--allow-writes` for the MCP server to grant — a stronger property than a
guarded write, and a cheaper one to verify.

### Why `detect` exists

A model asked *"does this repository do spec-driven development?"* answers
confidently either way. A filesystem check answers with a path or with nothing.

That matters because **a rule the project never adopted is not a finding**.
Pointing an auditor at a microservice and having it report that the service does
not follow practices it never claimed to is noise, and noise is how an auditor
teaches people to ignore it — including on the day it is right.

```
$ agentctl detect ~/some-service
code roots: .

  [ no] spec-driven
  [ no] debt-register
  [yes] agent-tooling  (.claude)
  [yes] declared-gates  (.gitlab-ci.yml)
  [yes] content-roots  (docs)

3 adopted, 3 not applicable
```

### Why `clipboard copy` is not one line of shell

Handing a value to a human through the clipboard is the one operation where "the
command exited 0" is worthless evidence. On X11 a selection lives only while the
owning process does, so setting it and exiting leaves the clipboard **empty**,
and the failure is invisible until somebody pastes. It has happened: an SSH key
handed over this way arrived as a blank entry.

So the backend is chosen by what is actually running — KDE's Klipper first,
because it takes ownership itself and keeps the entry in history — and the write
is read back wherever a backend can be read back. Non-ASCII goes through the raw
UTF-8 targets rather than the convenience API, which eats accents.

---

---

## The Slack bridge

[`apps/slack-bridge`](apps/slack-bridge) is a service rather than a plugin: a
Socket Mode listener that lets you drive Claude Code and Codex sessions from a
private Slack channel. Reply in a thread, the agent answers in that thread, and
the session survives — so you can pick one up from your phone.

```bash
uvx --from apps/slack-bridge slackbridge health
```

Everything is environment-driven; the tokens, the channel, the allowlist and the
agent binaries are all variables. It needs `tmux` and, on KDE, `qdbus` — which no
variable fixes, so [its README](apps/slack-bridge/README.md) says so up front.

---

## The skills

`SKILL.md` is a cross-vendor standard, so these run unmodified on Claude Code,
Codex CLI, Gemini CLI and Cursor.

| | |
|---|---|
| `verifying-a-claim` | fourteen ways a check passes while proving nothing, and the questions to answer before writing "verified", "blocked" or "ready" |
| `authoring-skills-and-subagents` | command vs skill vs subagent; a `description` that routes; declaring guarantees instead of requesting them |
| `vetting-third-party-tooling` | never install a third-party MCP — clone, read, score 1-10, adapt or escalate |
| `verified-state-contract` | every claim about a live system carries the commit it was measured against |
| `measuring-technical-debt` | the ratchet, and how a debt signal ends up unable to observe what it names |

## The subagents

Claude Code only: `permissionMode`, `tools` and `isolation` are its frontmatter,
and other agents have their own mechanisms.

| | |
|---|---|
| `@agent-repo-auditor` | audits a repository against the rules **it** wrote for itself, after detecting which of those rules it actually adopted. `permissionMode: plan` — reports, never edits |
| `@agent-research-analyst` | evaluates external tooling by cloning and reading it. Never installs, never executes a candidate |

---

## Install

**Claude Code** — the plugin brings the skills, the subagents *and* the MCP
server:

```bash
claude plugin marketplace add git@github.com:Wil12Fred/agent-plugins.git
claude plugin install agent-toolkit@agent-plugins     # user scope: every project
```

**Codex CLI / Gemini CLI** — the skills. Both read `~/.agents/skills/`, so one
set of links covers them:

```bash
git clone git@github.com:Wil12Fred/agent-plugins.git
cd agent-plugins && mkdir -p ~/.agents/skills
for s in plugins/agent-toolkit/skills/*/; do
  ln -sfn "$PWD/$s" ~/.agents/skills/"$(basename "$s")"
done
```

**The CLI on its own**, no agent involved:

```bash
uvx --from plugins/agent-toolkit agentctl detect .
uvx --from plugins/agent-toolkit'[clipboard]' agentctl clipboard copy "hello"
```

The clipboard's X11 fallback needs `klembord`, which is why it is an extra
rather than a dependency: the Klipper path needs nothing at all.

---

## Development

```bash
cd plugins/agent-toolkit
uv sync --all-extras
uv run pytest        # 39 tests
uv run ruff check src tests
uv run mypy src      # strict
```

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
