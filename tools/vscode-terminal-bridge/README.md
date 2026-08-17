# claude-terminal-bridge

A VS Code extension that injects text into **one specific** integrated terminal,
matched by the process id of the shell running in it.

That precision is the whole point. `workbench.action.terminal.sendSequence`
types into whichever terminal happens to be focused, which is a race the moment
you have more than one agent session open — the reply lands in the wrong one and
looks like the agent answering a question nobody asked.

## Install

```bash
ln -sfn "$PWD/tools/vscode-terminal-bridge" ~/.vscode/extensions/claude-terminal-bridge
```

Then reload VS Code. It is a symlink on purpose, so editing the source is
immediate.

**A symlink into a repository is a pointer nothing checks.** This one sat broken
for months after the directory it pointed at was moved: the manifest recorded
the move correctly, VS Code failed quietly, and no gate looked outside the
repository. If you move this folder, re-run the command above — and consider
that the general lesson, not a footnote about this extension.

## Provenance

Extracted from a private repository, where the only coupling was the `publisher`
field.
