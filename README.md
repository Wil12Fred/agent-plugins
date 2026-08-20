# agent-plugins

Tooling for coding agents: a **CLI**, an **MCP server**, five **skills**, two
**subagents**, and a **Slack bridge** that lets you drive Claude Code and Codex
sessions from a phone. Everything here is project-agnostic — no company, no
product, no host names, no ticket numbers.

```
tools/vscode-terminal-bridge/   a VS Code extension: type into one *specific* terminal

plugins/agent-toolkit/     a Claude Code plugin
├── src/agentctl/          the CLI and the MCP server
├── skills/                5 skills — SKILL.md, so Claude Code, Codex, Gemini and Cursor all read them
├── agents/                2 subagents — Claude Code only
└── tests/                 199 tests

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
| `plugins/agent-toolkit` | 199 |
| `apps/slack-bridge` | 68 unit + 10 e2e |
| `apps/cloudprobe` | 77 |
| `apps/opscore` | 49 |
| `apps/gpull` | 10 |
| `apps/jiractl` | 22 + 4 integration |

---

## The CLI

```bash
uvx --from plugins/agent-toolkit agentctl --help
```

| Command | Answers |
|---|---|
| `agentctl detect [path]` | which practices this repository has adopted, each with the paths that prove it |
| `agentctl rules [path]` | which rules this repository declared for **itself** — scope, exceptions, and with `--check` the ones that can be measured, plus the count of files each weighed |
| `agentctl portable [path]` | which code is **not** about this project and could move to a shared repo — with `--target`, which of it is already there and was copied rather than moved. Exits 7 on a duplicate |
| `agentctl strays [path]` | which executables sit outside the code directory, and which are declared exceptions rather than nobody's decision. Exits 7 when anything is undeclared |
| `agentctl clipboard copy` | put text on the clipboard **and verify it landed** |
| `agentctl android …` | boot a headless emulator, screenshot it, tap, type, read logcat, install an APK |
| `agentctl dev pdf\|pptx\|css\|mermaid` | extract a PDF's pages and images, **print HTML to PDF**, read and edit a PowerPoint deck, combine SVGs into a sprite, hue-shift a stylesheet, render Mermaid without a browser |
| `agentctl drive deliver` | split a deck per task and publish `<ticket>/<task>/` to a Drive folder, from a JSON plan |
| `agentctl mcp` | serve `detect` and `strays` as MCP tools over stdio |

Every command takes `--json` and answers with exactly one envelope, so a script
gets structured output and a meaningful exit code from the same binary a person
reads.

**The MCP server is read-only, and only four commands are on it.** `detect`,
`rules`, `portable` and `strays` read a repository and nothing else, so there is no `--allow-writes` to
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

### Why `portable` exists

A repository accumulates two kinds of code and stops telling them apart. Some
encodes what the business does; the rest is mechanism — a screenshot driver, a
parallelism calculator, a git hook — that would work anywhere. The second kind
is invisible, because it lives in the same directories as the first and nobody
re-reads a helper that already works.

```
$ agentctl portable . --target ~/src/shared --expect-language english

measured against 24 project term(s) from .agent-rules.toml
201 code file(s) weighed, 48 candidate(s)

  [DUPLICATE] src/jira/adf.py  (407 lines)
      already in the target as apps/jiractl/src/jiractl/adf.py
  [portable ] tools/shot.mjs  (121 lines)
  [after-edit] tools/lib/launch.mjs  (31 lines)
      comments read as spanish; the target repository requires english
```

The test is mechanical: how many of a file's lines name the project, using the
vocabulary declared in `.agent-rules.toml`. "Does this feel reusable?" gets a
confident answer either way; "how many of these 121 lines name the company" has
one answer and a reader who disagrees can go and count.

**The duplicates are the point.** Extraction by copying instead of moving fails
*silently* — both copies work, so nothing breaks, and the divergence surfaces
when a fix made in one does not reach the other. Getting that check right took
three passes, each defeated by a real file:

- **imports are excluded from the hash**, because a copy is repointed at the new
  namespace on its way out;
- **docstrings and comments are excluded too**, because taking the project's
  name out of the prose is the whole point of extracting it — one 407-line
  module was identical in every statement and differed in two docstring lines;
- **a duplicate bypasses the portability threshold**, because the copy left
  behind keeps accreting project references. The more diverged it is, the more
  important the finding, and the more certainly a threshold drops it.

Import lines are also counted apart from a file's substance in the report, since
they are a rename rather than a rewrite: "6 mentions" for a file needing one
mechanical edit reads like a file nobody should touch.

### Why `rules` exists

`detect` answers what a repository has *adopted*. `rules` answers what it has
*decided*, and no directory listing reveals that. "Every identifier and comment
is English, except text quoted into a ticket or a chat message" is a decision
somebody made; until it is declared, an auditor audits against its own defaults
instead of the project's.

```toml
# .agent-rules.toml
[[rule]]
name = "english-everywhere"
kind = "language"
expect = "english"
rule = "Comments, docstrings and identifiers are English."
exempt = ["specs/**", "**/jira-*.md", "**/slack-*.md"]
why = "Reviewers do not share one first language; quoted ticket text keeps its own."
```

Three properties it is built around, each one a way this normally goes wrong:

- **An exception is part of the rule.** `exempt` wins over `applies_to`, because
  an exception is written to carve something out; resolving the overlap the
  other way makes every exception silently inert.
- **`checked: 0` is not a pass.** Every run reports how many files each rule
  actually weighed. Without that number, "no violations" over an empty set looks
  exactly like a clean repository.
- **What cannot be measured is named, not skipped.** A `naming` or `process`
  rule is returned as text with its scope, and listed under `unmeasurable`, so
  the auditor reads it by hand instead of inheriting a silent pass.

Only `language` is measured today, by word frequency over **comments and
docstrings** — not identifiers, where keywords and library names dominate the
ratio and report a thoroughly Spanish file as English. Files with too little
text return a third answer, *not enough to judge*, distinct from both verdicts.

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

### Why `dev pptx` takes no dependency

A `.pptx` is a zip of XML, so reading one, pulling its artwork out and running a
find/replace over it are stdlib. The library people reach for, `python-pptx`, is
the right answer for *authoring* — adding slides, creating shapes, moving things
on a canvas — and the wrong thing to make every installing project carry for the
four operations here. It is used in this repository, once, as an independent
judge: it opens the edited deck in the test that proves the output is valid.

`export` is the one that gets used most: a deck becomes a folder with `index.md`,
`images/` and a manifest, so a requirement delivered as a `.pptx` can live in a
repository and be read, grepped and diffed like anything else in it. `html` is the one that earns its keep on a review deck: it lays the slides out
from the geometry the file already carries — each shape records its own offset and
extent in EMU, so placing them is unit conversion, not layout. Pictures, text with
its per-run size and colour, the connectors a reviewer points arrows with, groups
with their child-space transform, and the background inherited from the layout or
master. Chain it into `dev pdf from-html` and a 27-slide deck becomes a 27-page
PDF **with no LibreOffice at all**. It is an approximation and names its limits:
tables, charts and SmartArt are drawn as a labelled outline rather than dropped,
and text reflow is the part only a real engine does.

`pdf` is the other half of the same job — `export` gives you a deck's
*content* as markdown, `pdf` gives you what the slides *look like*, which is what
you want when the deck is a design review and the layout is the message. It is the
one command here that is not stdlib, because it needs a layout engine rather than a
parser; it drives LibreOffice and refuses by name when that is absent, with no
pure-Python fallback on offer — a substitute renderer produces a document that is
not what the deck looks like, and the reader cannot tell by looking.
`--images-dir` sends the images
somewhere gitignored while `index.md` keeps linking to them, and `--colors 256`
is the flag to reach for before `--max-width` — on a real 27-slide deck it took the
exported images from 20 MB to 7.4 MB **without losing a pixel of resolution**, and
resolution is what keeps an annotation readable.

Three properties of the format cost time if you assume otherwise, and each is
handled rather than documented:

- **`slide7.xml` is not the seventh slide.** Part names are assigned at creation
  and never renumbered, so any reordered deck disagrees with itself. The order
  is in `presentation.xml`, and `inspect` reads it from there.
- **`ppt/media/` is not the slides' artwork.** It holds every part's, the master
  and layouts included — which is where a client's logo almost always is. A flat
  listing over-reports; per-slide attribution loses the logo. Both are reported,
  separately.
- **A sentence is not a run.** PowerPoint splits a paragraph at every formatting
  change and at boundaries nothing on screen reveals, so `Total: 42` is
  routinely three runs and a per-run find/replace misses text that is plainly
  visible.

That last one has a second half, and only a real deck revealed it. Matching has
to see the joined paragraph; *writing* does not. Collapsing every multi-run
paragraph onto its first run's formatting flattened five paragraphs of a
27-slide deck that needed nothing of the sort — so the change goes into the
individual runs whenever that gives the same answer, and only a match that truly
crosses a boundary pays. The report names the ones that did.

The same deck produced the other bug worth recording: `identify` was being asked
for the dimensions of a 93 MB embedded mp4, so ImageMagick decoded the video and
the extraction did not return in two minutes. It read as a hang rather than as a
wrong answer. Rasters are now an allow-list, `-ping` stops at the header, and a
timeout backstops the rest — 1.2 seconds for the same 37 assets.

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

**[`jiractl`](apps/jiractl)** — JIRA Cloud, with the part that is actually hard:
its comment API takes **ADF**, a nested document tree, not text. Write markdown,
get headings, tables, code blocks and resolved `@mentions`. It also does the
obscure one — embedding an attachment *inside* a comment needs a media id that
is not in the upload response.

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
| `@agent-repo-auditor` | audits a repository against the rules **it** declared for itself — read from its own `.agent-rules.toml` and prose — after detecting which practices it actually adopted. `permissionMode: plan` — reports, never edits |
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

One gate over all six packages:

```bash
make check          # lint + typecheck + tests + plugin manifests — 425 tests
make integration    # the suites that touch this machine and the network
make fmt            # ruff format + fix
```

Each package is independent — its own `pyproject.toml`, its own tests — and the
apps share `opscore` through a uv workspace. `plugins/agent-toolkit` deliberately
does not: a Claude Code plugin is installed by copying its directory, so it has
to carry its own dependencies.

Everything is `mypy --strict` clean. How to work here, and the one rule that
governs it, is in [`AGENTS.md`](AGENTS.md).

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
