# agent-plugins

Tooling for coding agents: a **CLI**, an **MCP server**, five **skills**, two
**subagents**, and a **Slack bridge** that lets you drive Claude Code and Codex
sessions from a phone. Everything here is project-agnostic — no company, no
product, no host names, no ticket numbers.

```
plugins/agent-toolkit/     a Claude Code plugin
├── src/agentctl/          the CLI and the MCP server
├── skills/                5 skills — SKILL.md, so Claude Code, Codex, Gemini and Cursor all read them
├── agents/                2 subagents — Claude Code only
└── tests/                 57 tests

plugins/workstation/       a Claude Code plugin
└── skills/                2 skills — this Arch/KDE machine's own operations

apps/                      services and CLIs, sharing `opscore`
├── opscore/               errors, output, secrets, env, guard, http, sql guard
├── slack-bridge/          drive Claude Code and Codex sessions from a Slack thread
├── cloudprobe/            GKE + Cloudflare forensics, and a deploy watcher
└── gpull/                 read-only Gmail attachments and Drive/Sheets exports
```

| Package | Tests |
|---|---|
| `plugins/agent-toolkit` | 39 |
| `apps/slack-bridge` | 68 unit + 10 e2e |
| `apps/cloudprobe` | 72 |
| `apps/gpull` | 10 |

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
| `agentctl android …` | boot a headless emulator, screenshot it, tap, type, read logcat, install an APK |
| `agentctl mcp` | serve `detect` and `strays` as MCP tools over stdio |

Every command takes `--json` and answers with exactly one envelope, so a script
gets structured output and a meaningful exit code from the same binary a person
reads.

**The MCP server is read-only, and only two commands are on it.** `detect` and
`strays` read a repository and nothing else, so there is no `--allow-writes` to
grant — a stronger property than a guarded write and a cheaper one to verify.
`android tap` and `android text` obviously *do* act on a device, which is why
they are CLI-only: an agent that can type into a phone is a different trust
decision from one that can read a directory, and it should be made on purpose
rather than inherited.

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

### Why `android` is here

It is the one part of a mobile toolchain that is not about any particular app:
boot an emulator, look at a screenshot, tap, type, read the log. It came from a
repository where it drove one company's Flutter app; what was portable was the
adb layer, and what was not — the login flow, the coordinate map of specific
screens — stayed behind.

Three things it handles that silently corrupt a run otherwise:

- **coordinates are AVD-native**, so a tap read off a screenshot rendered
  smaller misses every target by the same ratio, which looks like the app
  ignoring you;
- **`adb shell input text` runs through the *device's* shell**, which eats
  `$ & ( ) ; ' "`. That corrupted a password once and the app answered "wrong
  credentials", indistinguishable from actually having the wrong one;
- **the soft keyboard shifts the layout**, so the tap after a type lands
  somewhere else unless the keyboard is hidden first.

Nothing is guessed: no default AVD, no default package filter, no default
project. Each refuses and names the variable.

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

## The services

Three CLIs under `apps/`, sharing `opscore` — one error taxonomy, one JSON
envelope, one write guard, written once instead of three times.

**[`cloudprobe`](apps/cloudprobe)** — read-only forensics for a GKE + Cloudflare
deployment: probe failures, logs, metrics, edge requests, the topology diagram,
and a watcher that answers *did the thing I just shipped make anything worse*.
Nothing has a default cluster or zone, on purpose: a wrong cluster does not
produce an error, it produces an answer.

**[`gpull`](apps/gpull)** — the two Google things the APIs make awkward:
exporting a private Sheet and downloading a Gmail attachment's bytes.

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

## This workstation

[`plugins/workstation`](plugins/workstation) is the odd one out: two skills about
**this Arch/KDE machine** rather than about any project — recovering the network
after a system update, pacman, and why KDE stops opening Slack notifications
after a Slack update.

It is here rather than in a work repository because it is about the machine, not
the employer. A colleague cloning a company repo should not inherit skills for a
desktop they do not run.

```bash
claude plugin install workstation@agent-plugins
```

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

Each package is independent — its own `pyproject.toml`, its own tests.

```bash
cd plugins/agent-toolkit && uv sync --all-extras && uv run pytest   # 57 tests
cd apps/slack-bridge     && uv sync                && uv run pytest   # 68 tests
```

Both are `ruff check` and `mypy --strict` clean.

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
