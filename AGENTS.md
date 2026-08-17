# AGENTS — how to work in this repository

This repository is **project-agnostic by construction**, and that constraint is
the only thing keeping it useful. Everything else here follows from it.

## The one rule

**Nothing in this repository may name a company, a product, a customer, a
hostname, a ticket key or a person's account.**

Not in code, not in a docstring, not in a test fixture, not in a default value.
It is checkable, so check it before you commit:

```bash
grep -rniE '<the names you must not use>' --include='*.py' --include='*.md' .
```

Two consequences that are easy to get wrong:

- **A default is a name.** `DEFAULT_CLUSTER = "prod-cluster-7"` is somebody's
  cluster. Read it from the environment and **refuse when it is unset** — see
  *Refuse rather than guess* below.
- **A test fixture is a name.** `api.acme.com` in an assertion is the same leak
  as in source, and it is the one that survives review because nobody reads
  fixtures.

## Refuse rather than guess

A wrong default does not produce an error. It produces **an answer**, and an
answer is what a reader trusts.

Measured cases from this repository:

- an unset cluster emitted `cluster_name=""`, which matches nothing and reads
  exactly like *"there were no such logs"*;
- a JIRA client that guessed a site would report *"issue not found"* for a
  ticket that exists.

So: no default host, no default cluster, no default project, no default AVD, no
default package filter. Each raises `ConfigError` **naming the variable that
fixes it**. An error that says what broke without saying what to do about it is
half an error.

Where a "no scope at all" mode is genuinely wanted, keep **three** states rather
than two — `None` means unset and refuses, `""` means deliberately everything, a
value means that value. Collapsing the first two turns a deliberate opt-out into
an accident the day the configured value goes missing.

## Layout

```
apps/            services and CLIs, sharing `opscore` through a uv workspace
├── opscore/     errors, output, secrets, env, guard, http, the SQL read-only guard
├── cloudprobe/  GKE + Cloudflare forensics
├── gpull/       Gmail attachments, Drive/Sheets exports
├── jiractl/     JIRA Cloud, markdown → ADF
└── slack-bridge/  drive agent sessions from a Slack thread

plugins/         Claude Code plugins, installed by copying their directory
├── agent-toolkit/  5 skills, 2 subagents, the `agentctl` CLI + MCP server
└── workstation/    2 skills about one Arch/KDE machine

tools/           things that are not Python
```

**`plugins/agent-toolkit` is deliberately not a workspace member.** A plugin is
installed by copying its directory, so it must carry its own dependencies; the
apps share `opscore` because they are installed as packages.

## The gates

```bash
make check          # lint + typecheck + tests + plugin manifests
make integration    # the suites that touch this machine and the network
make fmt            # ruff format + fix
```

`make check` is the bar. `mypy --strict` everywhere, no exceptions granted
without a comment saying which upstream package lacks annotations and why.

**Integration tests are opt-in and skip with a reason when unconfigured.** A red
that only means "not configured here" teaches people to ignore red. Four JIRA
tests were reclassified this way after extraction removed the `.env` that had
been quietly making them look like unit tests.

## Writing a test

Each test **names the rule it enforces** in its docstring, and where the rule
came from a real failure, says what the failure looked like. The point is not
ceremony: a test whose name is `test_parse_ok` tells a later reader nothing
about whether deleting it is safe.

**Include the control.** A suite where every case asserts the same direction
passes when the function always returns that answer. If you assert that
something is refused, assert somewhere that something else is allowed.

**A gate that has never failed is not proven, it is unmeasured.** Break it on
purpose once and watch it go red.

## Adding a package

1. `apps/<name>/` with its own `pyproject.toml`, `src/`, `tests/`, `README.md`.
2. Depend on `opscore` (`{ workspace = true }`) rather than re-vendoring
   errors, output or secrets. Three copies of an error taxonomy drift.
3. Add it to `APPS` in the `Makefile` — a package no gate runs is a package
   nobody has checked.
4. `--json` is a **root** option, before the subcommand. A flag that exists on
   some commands and not others is one a caller cannot loop over.
5. Entry point: load the `.env`, then catch `BridgeError` and render it as a
   message with its exit code. Without that, a missing variable reaches the
   terminal as a traceback pointing at `cli.py`, which reads as *this tool is
   broken* rather than *configure it*.

## Adding a skill or a subagent

`plugins/agent-toolkit/skills/<name>/SKILL.md`. The `description` is a **router**,
not a summary: when to use it, what it does concretely, and **when not to**,
naming the alternative. See the `authoring-skills-and-subagents` skill, which is
in this repository and is the canonical version of this advice.

Skills are a cross-vendor standard, so they run unmodified in Codex, Gemini and
Cursor. Subagents do not port — `permissionMode`, `tools` and `isolation` are
Claude Code frontmatter.

## Declaring a rule

This repository declares its own rules in `.agent-rules.toml`, and
`agentctl rules --check .` measures the ones that can be measured. Adding a rule
there is how you make it something an auditor enforces rather than something a
reader may notice.

Two obligations when you add one:

- **Write the exceptions.** A rule without its carve-outs is a different rule,
  and the auditor will hold the repository to the stricter one.
- **Say whether it is measurable.** Only `kind = "language"` is checked today.
  Everything else is carried to the auditor as text — which is worth having, but
  it is read, not verified, and the report must say which of the two happened.

## Before you write "verified"

Read the `verifying-a-claim` skill, which lives here. It is fourteen ways a
check passed while proving nothing, and this repository produced several of them
while being built:

- a `--yes` flag that is ignored inside an agent session, so the install *looked*
  scriptable;
- a `make` guard that could never pass, because `make -C` changes the recipe's
  working directory;
- a default bound at `def` time, so an environment variable set afterwards
  changed nothing and the code looked configurable while being fixed;
- three blind string replacements that each changed something adjacent to their
  target — a self-referential constant, a substring search inside an error
  message, and a stale nested command path in user-facing help.

Every one was caught by **running the thing**, not by reading it. Run it.
