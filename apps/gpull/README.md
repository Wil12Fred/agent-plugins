# gpull

Read-only Google access for the two things the APIs and the connected MCPs make
awkward: **exporting a private Sheet** and **downloading a Gmail attachment's
bytes**.

```bash
uvx --from apps/gpull gpull --help
```

| Command | |
|---|---|
| `gpull auth status` | which scopes you actually hold, and what is missing |
| `gpull auth consent` | run the Desktop OAuth flow once; the token goes to the OS keyring |
| `gpull auth token` | print an access token, for piping into `curl` |
| `gpull drive download` | export a private Sheet or download a Drive file |
| `gpull gmail attachments` / `download` | list a message's attachments, and fetch their bytes |

Global `--json` before the subcommand. **Nothing writes** — the only thing it
stores is your token, in the OS keyring, locally.

## Why it exists

The connected Gmail and Drive MCPs read messages and list attachments, but
neither hands you the **bytes**, and neither exports a Sheet. So the moment your
data lives in a private spreadsheet or an emailed file, you are stuck — and
`gcloud`'s shared OAuth client cannot rescue you:

- **`drive.readonly` is a *sensitive* scope.** It can be added to an existing
  `gcloud` login with `--enable-gdrive-access`, and after that tokens mint
  indefinitely with no further prompts.
- **`gmail.readonly` is a *restricted* scope**, and the same trick is refused.
  It needs a Desktop OAuth client in a project of your own, with the consent
  screen set to `User Type = Internal`.

`gpull auth status` tells you which of those you are in, with the exact command
to fix it, rather than making you find out from a scope error.

## Configuration

Everything is discovered or stored in the keyring; there is nothing to configure
before the first run except the OAuth client for Gmail, which `auth status`
walks you through.

## Development

```bash
cd apps/gpull
uv sync && uv run pytest        # 10 tests
uv run ruff check src tests && uv run mypy src
```

## Provenance

Extracted from a private repository. The coupling was one organisation's domain
and project names inside the consent instructions — the code itself never knew
whose account it was reading.
