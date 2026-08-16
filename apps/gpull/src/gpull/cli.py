"""``gpull`` — read-only Gmail and Drive access.

Two jobs, both closing a gap in the connected MCPs:

* **Gmail** — the MCP lists attachments but cannot download their bytes.
* **Drive** — migration source sheets are private, so an unauthenticated export
  returns a login page instead of the workbook.

Nothing in this package mutates anything on Google's side: no send, no label,
no upload, no write-back to Drive. The only state it ever writes is the local
OAuth credential in the OS keyring, via ``auth consent``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from opscore.output import get_output

from gpull import drive, gmail, oauth
from gpull.gmail import GmailClient, safe_filename

app = typer.Typer(
    name="google",
    help="Read-only Google access: Gmail attachment bytes, Drive/Sheets export.",
    no_args_is_help=True,
)

auth_app = typer.Typer(name="auth", help="OAuth state for Gmail and Drive.", no_args_is_help=True)
gmail_app = typer.Typer(name="gmail", help="Gmail attachments (read-only).", no_args_is_help=True)
drive_app = typer.Typer(
    name="drive", help="Drive / Sheets export (read-only).", no_args_is_help=True
)
app.add_typer(auth_app, name="auth")
app.add_typer(gmail_app, name="gmail")
app.add_typer(drive_app, name="drive")

TokenOption = Annotated[
    str | None,
    typer.Option("--token", help="Access token; otherwise resolved from env/keyring/gcloud."),
]


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


# --- auth --------------------------------------------------------------------
@auth_app.command("status")
def auth_status() -> None:
    """Report what credentials exist for Gmail and Drive, without revealing them.

    The two paths are unrelated: Drive rides on the ordinary gcloud login,
    Gmail needs the dedicated Internal OAuth client because `gmail.readonly` is
    a restricted scope Google will not grant to the shared gcloud client.
    """
    creds = oauth.load_credentials()
    gcloud_available = oauth.gcloud_token()
    drive_ok = gcloud_available is not None and oauth.has_drive_scope(gcloud_available)

    get_output().result(
        {
            "gmail": {
                "stored_client": creds.redacted() if creds else None,
                "ready": creds is not None,
                "hint": None if creds else oauth.SETUP_HINT,
            },
            "drive": {
                "gcloud_token": bool(gcloud_available),
                "drive_scope": drive_ok,
                "hint": (
                    None if drive_ok else "gcloud auth login --enable-gdrive-access --update-adc"
                ),
            },
        }
    )


@auth_app.command("consent")
def auth_consent(
    client_secret: Annotated[
        Path,
        typer.Option("--client-secret", help="Desktop OAuth client JSON downloaded from GCP."),
    ],
    scope: Annotated[str, typer.Option("--scope", help="Scope to request.")] = oauth.GMAIL_SCOPE,
    port: Annotated[int, typer.Option("--port", help="Loopback port for the redirect.")] = 8765,
) -> None:
    """Run the one-time Gmail consent flow and store the refresh token.

    Opens a browser; consent as the account that owns the data. Requires a Desktop
    OAuth client created in a project of your own whose consent screen is
    User Type = **Internal** — that is what lets an org app use the restricted
    `gmail.readonly` scope without Google verification.

    This writes only to the local OS keyring; no remote state changes,
    which is why it carries no prod-write guard.
    """
    message = oauth.run_consent(client_secret, scope=scope, port=port)
    get_output().result({"stored": True, "detail": message})


@auth_app.command("token")
def auth_token(
    service: Annotated[
        str, typer.Option("--service", help="Which token to mint: gmail | drive.")
    ] = "gmail",
) -> None:
    """Print a short-lived access token for piping into curl.

    The single command in this package that emits a secret, and only because
    that is its entire purpose. Never paste the output anywhere it is stored.
    """
    token = oauth.resolve_gmail_token() if service == "gmail" else oauth.resolve_drive_token()
    get_output().result(token)


# --- gmail -------------------------------------------------------------------
@gmail_app.command("attachments")
def list_attachments(
    message_id: Annotated[
        str, typer.Option("--message-id", help="Gmail message id (MCP get_thread messages[].id).")
    ],
    user: Annotated[str, typer.Option("--user", help="Mailbox to read.")] = "me",
    token: TokenOption = None,
) -> None:
    """List a message's attachments — filename, type, size and the id to download.

    The ids match what the Gmail MCP already reports, so a thread read through
    the MCP hands straight over to this command.
    """
    with GmailClient(oauth.resolve_gmail_token(token), user=user) as client:
        attachments = client.list_attachments(message_id)
    get_output().table(
        [a.as_dict() for a in attachments],
        columns=["filename", "mime_type", "size", "attachment_id"],
        title=f"{len(attachments)} attachment(s) on {message_id}",
    )


@gmail_app.command("download")
def download_attachment(
    message_id: Annotated[str, typer.Option("--message-id", help="Gmail message id.")],
    attachment_id: Annotated[
        str | None, typer.Option("--attachment-id", help="Single attachment to fetch.")
    ] = None,
    all_attachments: Annotated[
        bool, typer.Option("--all", help="Fetch every attachment on the message.")
    ] = False,
    out: Annotated[
        Path | None, typer.Option("--out", help="Destination file for a single download.")
    ] = None,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", help="Destination directory for --all.")
    ] = Path("."),
    user: Annotated[str, typer.Option("--user", help="Mailbox to read.")] = "me",
    token: TokenOption = None,
) -> None:
    """Download attachment bytes to disk.

    Pass ``--attachment-id`` for one, or ``--all`` to pull every attachment into
    ``--out-dir``. Filenames coming off the wire are sanitised before they are
    used as paths.
    """
    if not attachment_id and not all_attachments:
        raise typer.BadParameter("pass --attachment-id or --all")

    out_object = get_output()
    with GmailClient(oauth.resolve_gmail_token(token), user=user) as client:
        available = client.list_attachments(message_id)
        if all_attachments:
            targets = available
        else:
            targets = [a for a in available if a.attachment_id == attachment_id]
            if not targets and attachment_id:
                # The parts walk misses inline attachments on some messages —
                # the script this replaced called those "rare inline cases" and
                # tried the id anyway rather than refusing. Gmail answers if the
                # id is real, and 404s if it is not, so attempting costs one
                # call and refusing costs the user their attachment.
                out_object.warn(
                    f"{attachment_id} is not in the parts listing; trying it anyway "
                    "(inline attachments are not always listed)"
                )
                targets = [
                    gmail.Attachment(
                        filename="",
                        mime_type="application/octet-stream",
                        attachment_id=attachment_id,
                        size=0,
                    )
                ]

        written: list[dict[str, object]] = []
        for index, attachment in enumerate(targets):
            content = client.download(message_id, attachment.attachment_id)
            if all_attachments or out is None:
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / safe_filename(attachment.filename, f"attachment_{index}")
            else:
                path = out
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            written.append(
                {"path": str(path), "bytes": len(content), "mime_type": attachment.mime_type}
            )
            out_object.step(f"{len(content)} bytes → {path}")

    out_object.result({"message_id": message_id, "downloaded": written})


# --- drive -------------------------------------------------------------------
@drive_app.command("info")
def drive_info(
    file: Annotated[str, typer.Option("--file", help="Drive file id or a pasted Sheets URL.")],
    token: TokenOption = None,
) -> None:
    """Show a Drive file's metadata — also the cheapest proof that access works."""
    file_id = drive.extract_file_id(file)
    meta = drive.get_metadata(file_id, oauth.resolve_drive_token(token))
    get_output().result(meta.as_dict())


@drive_app.command("download")
def drive_download(
    file: Annotated[str, typer.Option("--file", help="Drive file id or a pasted Sheets URL.")],
    out: Annotated[Path, typer.Option("--out", help="Destination path.")],
    export_format: Annotated[
        drive.ExportFormat,
        typer.Option("--format", help="Export format for a native Google Sheet."),
    ] = drive.ExportFormat.XLSX,
    token: TokenOption = None,
) -> None:
    """Download a Drive file; a native Google Sheet is exported first.

    This is how a `MigraciónAvanzada` source sheet reaches the workstation.
    ``--format xlsx`` keeps every tab, which matters because the migrator tool
    reads the **first** one — export to CSV and that choice is made for you.

    Read-only: nothing is written back to the user's Drive.
    """
    file_id = drive.extract_file_id(file)
    meta = drive.download(
        file_id, out, oauth.resolve_drive_token(token), export_format=export_format
    )
    get_output().result(
        {
            "file_id": meta.file_id,
            "name": meta.name,
            "native_sheet": meta.is_native_sheet,
            "format": export_format.value if meta.is_native_sheet else meta.mime_type,
            "path": str(out),
            "bytes": out.stat().st_size,
        }
    )


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
