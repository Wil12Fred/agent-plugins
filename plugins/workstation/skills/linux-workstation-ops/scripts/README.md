# scripts/

Standalone helpers that back this skill's procedures, kept as real files rather
than fenced blocks inside `SKILL.md` so they can be run, linked and tested. This
is the canonical `scripts/` subdirectory of the skill: it travels with the skill
on both install paths — a Claude Code plugin is installed by copying its
directory, and `make install-agents` symlinks each skill directory into
`~/.agents/skills/` for Codex and Gemini — so a script here is available wherever
the skill is loaded.

Install one onto your `PATH` by symlinking it, which keeps a single source of
truth (editing the repo edits the tool):

```bash
ln -sfn "$PWD/<script>" ~/.local/bin/<script>
```

## Index

| Script | Does | Doc |
|---|---|---|
| [`camctl`](camctl) | List video-capture devices (webcams) and enable/disable one per device via the USB `authorized` flag — without touching the driver of the others. | [`camctl.md`](camctl.md) |

## Conventions

- **English only, no proper nouns** — same as the rest of the repository. IDs like
  `093a:2700` or nodes like `3-7` appear only as generic examples.
- **Each script carries its own doc** as a sibling `<script>.md`: what it does, how
  it works, and any privilege it needs.
- **Privileged actions use `sudo`** for the one write they need and nothing more; a
  narrow `/etc/sudoers.d` rule (documented per script) can make them passwordless.
