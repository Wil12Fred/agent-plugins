"""Gmail attachment bytes — the one thing the Gmail MCP cannot do.

The connected Gmail MCP reads messages and threads and reports each attachment
as metadata only (``{filename, id, mimeType}``, plus ``attachmentIds[]`` on the
message). It exposes no download tool, so binary attachments — video, PDF, an
image sent as a file — cannot be retrieved through it at all.

This module closes that gap with the Gmail REST API, consuming the very ids the
MCP already surfaces::

    MCP get_thread → messages[].id               == message_id
                     messages[].attachments[].id == attachment_id

Strictly read-only: it calls ``GET messages/{id}`` and
``GET messages/{id}/attachments/{id}``, and nothing else. No send, no label, no
trash, no modify.

Frictionless alternative when the OAuth setup is not in place: click
**"Save to Drive"** on the attachment in Gmail — the Drive scope is already
consented — and then read it with ``gpull drive download``.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opscore.errors import NotFoundError
from opscore.http import HttpClient

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"

# Ask only for the parts that carry attachment metadata; a full message body is
# megabytes of quoted HTML nobody here needs. Three levels of nesting covers
# the multipart/mixed → multipart/alternative → part shape Gmail produces.
_PART_FIELDS = "filename,mimeType,body/attachmentId,body/size"
MESSAGE_FIELDS = f"payload({_PART_FIELDS},parts({_PART_FIELDS},parts({_PART_FIELDS})))"

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._@ +-]")


@dataclass(frozen=True)
class Attachment:
    """One downloadable part of a message."""

    filename: str
    mime_type: str
    attachment_id: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename or "(no name)",
            "mime_type": self.mime_type,
            "size": self.size,
            "attachment_id": self.attachment_id,
        }


def safe_filename(name: str, fallback: str) -> str:
    """Make a mail-supplied filename safe to write.

    A filename off the wire is attacker-controlled: it can contain ``../`` or a
    leading ``/``. Only the basename survives, and the rest is scrubbed.
    """
    candidate = Path((name or "").strip()).name
    candidate = _UNSAFE_FILENAME.sub("_", candidate)
    return candidate or fallback


def iter_attachments(payload: dict[str, Any]) -> Iterator[Attachment]:
    """Walk a (possibly deeply nested) message payload for attachment parts."""
    stack: list[dict[str, Any]] = [payload]
    while stack:
        part = stack.pop()
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        if attachment_id:
            yield Attachment(
                filename=str(part.get("filename") or ""),
                mime_type=str(part.get("mimeType") or "application/octet-stream"),
                attachment_id=str(attachment_id),
                size=int(body.get("size") or 0),
            )
        stack.extend(part.get("parts") or [])


def decode_attachment(data: str) -> bytes:
    """Decode Gmail's base64url payload, which arrives without padding."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class GmailClient:
    """Read-only Gmail REST client scoped to attachment retrieval."""

    def __init__(self, token: str, *, user: str = "me") -> None:
        self.user = user
        self._http = HttpClient(base_url=GMAIL_API, token=token)

    def list_attachments(self, message_id: str) -> list[Attachment]:
        """Enumerate a message's attachments without downloading any bytes."""
        message = self._http.get(
            f"/users/{self.user}/messages/{message_id}",
            params={"format": "full", "fields": MESSAGE_FIELDS},
        )
        return list(iter_attachments(message.get("payload") or {}))

    def download(self, message_id: str, attachment_id: str) -> bytes:
        """Fetch one attachment's decoded bytes."""
        response = self._http.get(
            f"/users/{self.user}/messages/{message_id}/attachments/{attachment_id}"
        )
        data = response.get("data")
        if data is None:
            raise NotFoundError(
                f"attachment {attachment_id[:16]}… returned no data field",
                detail="the id may belong to another message",
            )
        return decode_attachment(str(data))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GmailClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
