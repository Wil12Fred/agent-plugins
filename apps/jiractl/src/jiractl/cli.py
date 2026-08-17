"""``jiractl`` — read tickets, post ADF comments, attach evidence, transition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from opscore.errors import NotFoundError, ValidationError
from opscore.guard import Consequence, WriteIntent, check_write
from opscore.output import get_output

from jiractl import adf, attachments, config, mentions
from jiractl.client import JiraClient

app = typer.Typer(
    name="jira",
    help="JIRA: read issues, post ADF comments (markdown in, mentions resolved), attach files.",
    no_args_is_help=True,
)


@app.callback()
def _root(
    json_mode: Annotated[
        bool, typer.Option("--json", help="Emit exactly one JSON envelope on stdout.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Suppress progress on stderr.")] = False,
) -> None:
    """Options every command shares. `--json` goes before the subcommand."""
    from opscore.output import Output, set_output

    set_output(Output(json_mode=json_mode, quiet=quiet))


def _summarise(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields", {})
    status = fields.get("status") or {}
    assignee = fields.get("assignee") or {}
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": status.get("name"),
        "assignee": assignee.get("displayName"),
        "url": config.load().browse(str(issue.get("key"))),
    }


@app.command("get")
def get_issue(
    key: Annotated[str, typer.Argument(help="Issue key, e.g. PROJ-123.")],
    full: Annotated[bool, typer.Option("--full", help="Return every field.")] = False,
) -> None:
    """Fetch one issue."""
    with JiraClient() as client:
        issue = client.get_issue(key)
    get_output().result(issue if full else _summarise(issue))


@app.command("search")
def search(
    jql: Annotated[str, typer.Argument(help="JQL query.")],
    limit: Annotated[int, typer.Option("--limit", help="Maximum issues to return.")] = 50,
) -> None:
    """Search issues with JQL."""
    with JiraClient() as client:
        issues = client.search(jql, limit=limit)
    get_output().table(
        [_summarise(i) for i in issues],
        columns=["key", "status", "assignee", "summary"],
        title=f"{len(issues)} issue(s)",
    )


@app.command("comment")
def comment(
    key: Annotated[str, typer.Argument(help="Issue key to comment on.")],
    file: Annotated[
        Path | None,
        typer.Option("--file", help="Markdown file to convert to ADF and post."),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option("--body", help="Markdown text to post (alternative to --file)."),
    ] = None,
    mention: Annotated[
        list[str] | None,
        typer.Option("--mention", help="Tag someone: alias, name, email or accountId."),
    ] = None,
    attach: Annotated[
        list[Path] | None,
        typer.Option("--attach", help="Upload and embed a file inline in the comment."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Render the ADF without posting.")
    ] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm-prod-write", help="Actually post the comment.")
    ] = False,
) -> None:
    """Post a comment, authored as markdown.

    The ticket folders already hold comments as markdown files, so the usual
    flow is ``--file specs/<KEY>/jira-comment-summary.md``. Mentions are
    resolved to account ids and prepended; attachments are uploaded and embedded
    as media cards in the body, not just dropped in the attachment panel.
    """
    if (file is None) == (body is None):
        raise ValidationError("pass exactly one of --file or --body")

    markdown = file.read_text(encoding="utf-8") if file else (body or "")
    if file and not file.is_file():
        raise NotFoundError(f"comment file not found: {file}")

    out = get_output()
    with JiraClient() as client:
        people = mentions.resolve_all(client, list(mention or []))
        document = adf.from_markdown(markdown)

        if people:
            greeting: list[dict[str, Any]] = []
            for index, person in enumerate(people):
                if index:
                    greeting.append(adf.text(" "))
                greeting.append(adf.mention(person.account_id, person.mention_text))
            document["content"].insert(0, adf.paragraph(*greeting))

        uploaded: list[attachments.Attachment] = []
        if attach and not dry_run and confirm:
            for path in attach:
                item = attachments.upload(client, key, path)
                uploaded.append(item)
                if item.media_id:
                    document["content"].append(adf.media_card(item.media_id))
                else:
                    out.warn(f"{item.filename} uploaded but has no media id; not embedded inline")

        intent = WriteIntent(consequence=Consequence.EXTERNAL, action="post a comment", target=key)
        if not check_write(intent, dry_run=dry_run, confirmed=confirm):
            out.result(
                {
                    "issue": key,
                    "mentions": [p.as_dict() for p in people],
                    "attachments": [str(p) for p in attach or []],
                    "adf": document,
                }
            )
            return

        created = client.add_comment(key, document)

    out.result(
        {
            "issue": key,
            "comment_id": created.get("id"),
            "url": config.load().browse(key),
            "mentions": [p.as_dict() for p in people],
            "attachments": [a.as_dict() for a in uploaded],
        }
    )


@app.command("comments")
def list_comments(
    key: Annotated[str, typer.Argument(help="Issue key.")],
    limit: Annotated[int, typer.Option("--limit", help="How many comments.")] = 10,
) -> None:
    """List an issue's comments (author and date; bodies are ADF)."""
    with JiraClient() as client:
        items = client.list_comments(key, limit=limit)
    get_output().table(
        [
            {
                "id": c.get("id"),
                "author": (c.get("author") or {}).get("displayName"),
                "created": c.get("created"),
            }
            for c in items
        ],
        columns=["id", "author", "created"],
        title=f"{key}: {len(items)} comment(s)",
    )


@app.command("transitions")
def list_transitions(key: Annotated[str, typer.Argument(help="Issue key.")]) -> None:
    """List the workflow transitions available on an issue."""
    with JiraClient() as client:
        items = client.transitions(key)
    get_output().table(
        [{"id": t.get("id"), "name": t.get("name")} for t in items],
        columns=["id", "name"],
        title=f"{key}: transitions",
    )


@app.command("transition")
def transition(
    key: Annotated[str, typer.Argument(help="Issue key.")],
    transition_id: Annotated[str, typer.Option("--to", help="Transition id (see `transitions`).")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Do not apply.")] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm-prod-write", help="Actually transition.")
    ] = False,
) -> None:
    """Move an issue through the workflow.

    Remember what the statuses mean: a ticket waiting on an answer from CS or
    the client goes to `CS - Cliente` (4), never `CS - QA PROD` (2) — that one
    says "it is deployed, please validate".
    """
    intent = WriteIntent(
        consequence=Consequence.EXTERNAL, action=f"transition to {transition_id}", target=key
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result({"issue": key, "transition": transition_id, "applied": False})
        return
    with JiraClient() as client:
        client.transition(key, transition_id)
    get_output().result({"issue": key, "transition": transition_id, "applied": True})


@app.command("whois")
def whois(query: Annotated[str, typer.Argument(help="Alias, display name or email.")]) -> None:
    """Resolve a person to the accountId a mention needs."""
    with JiraClient() as client:
        person = mentions.resolve(client, query)
    get_output().result(person.as_dict())


@app.command("attach")
def attach_file(
    key: Annotated[str, typer.Argument(help="Issue key.")],
    path: Annotated[Path, typer.Argument(help="File to upload.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Do not upload.")] = False,
    confirm: Annotated[bool, typer.Option("--confirm-prod-write", help="Actually upload.")] = False,
) -> None:
    """Upload a file and report the media id needed to embed it in a comment."""
    intent = WriteIntent(consequence=Consequence.EXTERNAL, action=f"attach {path.name}", target=key)
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result({"issue": key, "file": str(path), "uploaded": False})
        return
    with JiraClient() as client:
        uploaded = attachments.upload(client, key, path)
    get_output().result(uploaded.as_dict())


@app.command("attachments")
def list_attachments(key: Annotated[str, typer.Argument(help="Issue key.")]) -> None:
    """List an issue's attachments."""
    with JiraClient() as client:
        items = attachments.list_for_issue(client, key)
    get_output().table(
        items, columns=["attachment_id", "filename", "size", "created"], title=f"{key}"
    )


@app.command("projects")
def list_projects() -> None:
    """List the visible JIRA projects."""
    with JiraClient() as client:
        items = client.projects()
    get_output().table(
        [{"key": p.get("key"), "name": p.get("name")} for p in items],
        columns=["key", "name"],
        title=f"{len(items)} project(s)",
    )


@app.command("adf")
def render_adf(
    file: Annotated[Path, typer.Argument(help="Markdown file to convert.")],
) -> None:
    """Convert markdown to ADF and print it — useful to review before posting."""
    if not file.is_file():
        raise NotFoundError(f"file not found: {file}")
    document = adf.from_markdown(file.read_text(encoding="utf-8"))
    out = get_output()
    if out.json_mode:
        out.result(document)
    else:
        out.result(document, human=json.dumps(document, indent=2, ensure_ascii=False))


@app.command("create")
def create_issue(
    project: Annotated[
        str, typer.Option("--project", help="Project key, e.g. OPER. AGENTS.md requires it.")
    ],
    summary: Annotated[str, typer.Option("--summary", help="Issue title, in Spanish.")],
    assignee: Annotated[
        str,
        typer.Option("--assignee", help="Alias, email or accountId. AGENTS.md requires one."),
    ],
    file: Annotated[
        Path | None,
        typer.Option("--file", help="Description as markdown; converted to ADF."),
    ] = None,
    body: Annotated[str | None, typer.Option("--body", help="Inline markdown description.")] = None,
    issue_type: Annotated[str, typer.Option("--type", help="Issue type name.")] = "Task",
    field: Annotated[
        list[str] | None,
        typer.Option(
            "--field",
            help="Extra field as name=value, repeatable. The value is parsed as JSON when it "
            "looks like it: --field customfield_10020=[2041] sends a sprint id as an array.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Render the fields without creating.")
    ] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm-prod-write", help="Actually create the issue.")
    ] = False,
) -> None:
    """Create an issue, with its description authored as markdown.

    `JiraClient.create_issue` shipped with the port but nothing called it, so
    `create-ticket.sh` — whose whole purpose was `POST /rest/api/3/issue` — had
    no replacement even though the manifest said it did.

    `--project` and `--assignee` are both **required**, because AGENTS.md makes
    them mandatory for a new ticket: an issue created without an owner is one
    nobody picks up. Placement (sprint vs backlog) goes through `--field`, since
    the custom field id differs per project — `--field customfield_10020=[2041]`
    for OPER.

    JIRA starts every issue at its workflow's first status, so the initial
    status AGENTS.md also asks for is set afterwards with `jiractl
    transition`.

    The description is markdown for the same reason comments are: the ticket
    folders already hold it that way.
    """
    if (file is None) == (body is None):
        raise ValidationError("pass exactly one of --file or --body")
    if file and not file.is_file():
        raise NotFoundError(f"description file not found: {file}")
    markdown = file.read_text(encoding="utf-8") if file else (body or "")

    out = get_output()
    with JiraClient() as client:
        fields: dict[str, Any] = {
            "project": {"key": project},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "description": adf.from_markdown(markdown),
        }
        resolved = mentions.resolve_all(client, [assignee])
        if not resolved:
            raise NotFoundError(f"could not resolve an assignee for {assignee!r}")
        if len(resolved) > 1:
            raise ValidationError(
                f"{assignee!r} matches {len(resolved)} people: "
                + ", ".join(p.mention_text for p in resolved),
                detail="pass the accountId to disambiguate",
            )
        fields["assignee"] = {"accountId": resolved[0].account_id}

        for entry in field or []:
            name, _, value = entry.partition("=")
            if not value:
                raise ValidationError(f"--field expects name=value, got {entry!r}")
            # Sprint is `customfield_10020` and takes an array of ints, not the
            # string "2041" — the one example the help text gives is the one a
            # plain string cannot satisfy. Anything that parses as JSON is sent
            # as JSON; everything else stays a string.
            try:
                fields[name.strip()] = json.loads(value)
            except json.JSONDecodeError:
                fields[name.strip()] = value

        intent = WriteIntent(
            consequence=Consequence.EXTERNAL,
            action=f"create a {issue_type} in {project}",
            target=summary,
        )
        if not check_write(intent, dry_run=dry_run, confirmed=confirm):
            out.result({"fields": fields, "created": False})
            return
        created = client.create_issue(fields)
        browse = f"{client.base_url}/browse/{created.get('key')}" if created.get("key") else None

    out.result({"issue": created.get("key"), "created": True, "url": browse})


def main() -> None:
    """Entry point. Loads the `.env`, then renders our own errors as messages."""
    from opscore.env import load_env_file
    from opscore.errors import BridgeError

    load_env_file()
    try:
        app()
    except BridgeError as exc:
        get_output().failure(exc)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        raise SystemExit(130) from None
