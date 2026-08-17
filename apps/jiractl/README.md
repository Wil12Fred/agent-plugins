# jiractl

JIRA Cloud from the terminal. Read tickets, post comments **written as markdown
and rendered as ADF**, attach evidence and embed it inline, mention people by
name rather than by account id.

```bash
uvx --from apps/jiractl jiractl --json get PROJ-123
```

| Command | |
|---|---|
| `get` / `search` | one issue, or JQL |
| `comment` | post a comment authored as markdown — the ADF conversion is the point |
| `comments` | list an issue's comments |
| `transitions` / `transition` | see and move the workflow |
| `whois` | resolve a person to the `accountId` a mention needs |
| `attach` / `attachments` | upload a file and get the media id that embeds it *inside* a comment |
| `projects` | the projects you can see |
| `adf` | convert markdown to ADF and print it, to review before posting |
| `create` | create an issue, assignee required |

Global `--json` before the subcommand.

## Configuration

| | |
|---|---|
| `JIRA_SITE` | the host only, e.g. `acme.atlassian.net` |
| `JIRA_EMAIL` | the account the token belongs to — Basic auth pairs the two |
| `JIRA_TOKEN` | an API token, or store it in the keyring as `atlassian`/`api-token` |

**None has a default.** A JIRA client that guesses a site reports "issue not
found" for a ticket that exists, which is the worst answer available because it
looks like a real one.

## Why the ADF part is worth having

JIRA Cloud's comment API does not take text. It takes **ADF** — a nested
document tree where a bold word, a link, a code block and a mention are each a
different node shape. Writing that by hand is how a comment ends up as one
unstyled paragraph with a raw URL in it.

`jiractl comment --file notes.md` converts markdown to ADF: headings, lists,
tables, code blocks, links, and `@name` mentions resolved to account ids. `adf`
prints the tree without posting, so you can look before you send.

It also handles the thing that is genuinely obscure: **embedding an attachment
*inside* a comment**. Uploading a file puts it in the issue's attachment panel;
showing it in the comment body needs the file's *media id*, which is not in the
upload response — it comes from a redirect on the content URL. `attach` does
that lookup and reports the id.

## Development

```bash
cd apps/jiractl
uv sync && uv run pytest                  # 22 offline
uv run pytest -m integration              # 4, need JIRA_SITE and a token
uv run ruff check src tests && uv run mypy src
```

## Provenance

Extracted from a private repository. The coupling was one company's JIRA host,
hardcoded in two URL builders; everything else — the ADF conversion, the mention
resolution, the media-id lookup — never knew whose JIRA it was.

Four tests moved from the default run to `-m integration`. They shell out to the
binary and resolve an assignee, which is an API call, so they were only ever
"unit" tests because a configured `.env` happened to sit next to them.
