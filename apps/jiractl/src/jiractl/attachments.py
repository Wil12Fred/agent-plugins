"""Attachments, and how to embed one *inside* a comment.

Uploading a file gets you an issue-level attachment shown in the panel at the
bottom of the ticket. Putting the file **in the comment body** — a CSV the
reader can see without scrolling away — needs an ADF media card, and a media
card is addressed by a *media UUID*, which is not the attachment id.

The UUID is only exposed as a redirect: requesting the attachment's ``content``
URL without following redirects returns a ``Location`` whose path contains it.
That is the whole trick, and it is why this module talks raw HTTP instead of
going through the JSON client.

Note the panel copy is unavoidable: deleting the issue-level attachment breaks
the inline card, because the card points at that same file.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from opscore.errors import ApiError, NotFoundError

from jiractl.client import API_V3, JiraClient

UPLOAD_TIMEOUT = 120.0

# .../file/<uuid>/binary  — the media UUID lives in the redirect target.
_MEDIA_UUID = re.compile(r"/file/([0-9a-f-]{36})", re.IGNORECASE)


@dataclass(frozen=True)
class Attachment:
    """An uploaded file, with the id needed to embed it in a comment."""

    attachment_id: str
    filename: str
    size: int
    media_id: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "size": self.size,
            "media_id": self.media_id,
            "embeddable": self.media_id is not None,
        }


def upload(client: JiraClient, key: str, path: Path) -> Attachment:
    """Attach ``path`` to issue ``key`` and resolve its media UUID."""
    if not path.is_file():
        raise NotFoundError(f"file not found: {path}")

    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with httpx.Client(timeout=UPLOAD_TIMEOUT) as http:
        response = http.post(
            f"{client.base_url}{API_V3}/issue/{key}/attachments",
            headers={
                "Authorization": client.auth_header,
                # Required by JIRA for any multipart upload.
                "X-Atlassian-Token": "no-check",
            },
            files={"file": (path.name, path.read_bytes(), content_type)},
        )
        if response.status_code >= 400:
            raise ApiError(
                f"attachment upload failed for {key}",
                status_code=response.status_code,
                body=response.text[:500],
            )
        payload = response.json()
        if not payload:
            raise ApiError("attachment upload returned an empty response")

        entry = payload[0]
        media_id = _resolve_media_id(client, http, entry.get("content", ""))

    return Attachment(
        attachment_id=str(entry.get("id", "")),
        filename=str(entry.get("filename", path.name)),
        size=int(entry.get("size", 0)),
        media_id=media_id,
    )


def _resolve_media_id(client: JiraClient, http: httpx.Client, content_url: str) -> str | None:
    """Follow the content URL one hop and pull the media UUID out of the redirect."""
    if not content_url:
        return None
    response = http.get(
        content_url,
        headers={"Authorization": client.auth_header},
        follow_redirects=False,
    )
    location = response.headers.get("location", "")
    match = _MEDIA_UUID.search(location)
    return match.group(1) if match else None


def list_for_issue(client: JiraClient, key: str) -> list[dict[str, object]]:
    """List an issue's attachments."""
    issue = client.get_issue(key, fields="attachment")
    attachments = issue.get("fields", {}).get("attachment", []) or []
    return [
        {
            "attachment_id": a.get("id"),
            "filename": a.get("filename"),
            "size": a.get("size"),
            "created": a.get("created"),
        }
        for a in attachments
    ]
