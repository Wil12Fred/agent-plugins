"""Drive export — how a migration source sheet gets onto this machine.

Every `MigraciónAvanzada` ticket carries its data as **private** Google Sheets,
one per sede. They are not shared "anyone with the link", so the obvious
unauthenticated export URL returns HTTP 401 with an HTML login page — which,
saved to disk, looks like a corrupt .xlsx rather than an auth failure. The file
must be fetched from the Drive API with a Bearer token
(see :mod:`gpull.oauth` for how that token is obtained).

A Google Sheet is not a file with bytes: it has to be *exported* to a concrete
format. ``/export?mimeType=…openxmlformats…spreadsheetml.sheet`` produces the
whole workbook as .xlsx — which is what the migrator tool consumes, and what
``gpull migrator validate`` then checks. ``text/csv`` exports the first tab only.

Read-only, by policy: export and metadata, never a write back to the user's
Drive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from opscore.errors import ApiError, ValidationError

DRIVE_API = "https://www.googleapis.com/drive/v3"
DOWNLOAD_TIMEOUT = 300.0
"""A sede workbook can be tens of MB; the default 30s ceiling is not enough."""

_SHEET_URL_ID = re.compile(r"/(?:spreadsheets|file)/d/([A-Za-z0-9_-]{20,})")


class ExportFormat(StrEnum):
    """Export targets that matter for migration work."""

    XLSX = "xlsx"
    """Whole workbook, every tab. What a migration load needs."""

    CSV = "csv"
    """First tab only — Drive's export cannot pick a tab."""

    PDF = "pdf"


EXPORT_MIME: dict[ExportFormat, str] = {
    ExportFormat.XLSX: ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ExportFormat.CSV: "text/csv",
    ExportFormat.PDF: "application/pdf",
}


@dataclass(frozen=True)
class DriveFile:
    """Drive metadata for one file."""

    file_id: str
    name: str
    mime_type: str
    modified_time: str | None = None
    size: int | None = None

    @property
    def is_native_sheet(self) -> bool:
        """Native Sheets must be exported; an uploaded .xlsx is downloaded."""
        return self.mime_type == "application/vnd.google-apps.spreadsheet"

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "modified_time": self.modified_time,
            "size": self.size,
            "native_sheet": self.is_native_sheet,
        }


def extract_file_id(reference: str) -> str:
    """Accept either a bare file id or a pasted Sheets/Drive URL.

    Tickets link the sheet, they do not quote its id, so pasting the URL has to
    work or every use starts with manual surgery on a query string.
    """
    reference = reference.strip()
    if match := _SHEET_URL_ID.search(reference):
        return match.group(1)
    if "/" in reference or " " in reference:
        raise ValidationError(
            f"could not find a Drive file id in {reference!r}",
            detail="pass the id, or a full docs.google.com/spreadsheets/d/<id>/… URL",
        )
    return reference


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def get_metadata(file_id: str, token: str) -> DriveFile:
    """Read a file's metadata (also the cheapest check that access works)."""
    try:
        response = httpx.get(
            f"{DRIVE_API}/files/{file_id}",
            params={"fields": "id,name,mimeType,modifiedTime,size", "supportsAllDrives": "true"},
            headers=_headers(token),
            timeout=60.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise ApiError(f"Drive metadata request failed: {exc}") from exc
    if response.status_code >= 400:
        raise ApiError(
            f"Drive returned {response.status_code} for file {file_id}",
            status_code=response.status_code,
            body=_explain(response),
        )
    payload = response.json()
    size = payload.get("size")
    return DriveFile(
        file_id=str(payload.get("id", file_id)),
        name=str(payload.get("name", "")),
        mime_type=str(payload.get("mimeType", "")),
        modified_time=payload.get("modifiedTime"),
        size=int(size) if size is not None else None,
    )


def _explain(response: httpx.Response) -> str:
    """Turn Drive's answer into something worth reading.

    A 401 here arrives as an HTML sign-in page, and quoting 500 characters of
    markup helps nobody; say what it actually means instead.
    """
    if response.status_code == 401:
        return (
            "the token was rejected — the sheet is private and the gcloud login may lack the "
            "Drive scope (gcloud auth login --enable-gdrive-access --update-adc)"
        )
    if response.status_code == 404:
        return "no such file, or this account cannot see it — ask the ticket owner to share it"
    body = response.text[:300]
    return body if "<html" not in body.lower() else "(HTML response, not JSON)"


def download(
    file_id: str,
    destination: Path,
    token: str,
    *,
    export_format: ExportFormat | None = ExportFormat.XLSX,
) -> DriveFile:
    """Fetch a Drive file to ``destination``.

    A native Google Sheet is exported to ``export_format``; anything else (an
    already-uploaded .xlsx, a PDF) is downloaded verbatim with ``alt=media``,
    because ``/export`` refuses non-native files.

    Returns:
        The file's metadata, so the caller can report name and size.
    """
    meta = get_metadata(file_id, token)
    if meta.is_native_sheet:
        if export_format is None:
            raise ValidationError("a native Google Sheet must be exported to a format")
        url = f"{DRIVE_API}/files/{file_id}/export"
        params: dict[str, str] = {"mimeType": EXPORT_MIME[export_format]}
    else:
        url = f"{DRIVE_API}/files/{file_id}"
        params = {"alt": "media", "supportsAllDrives": "true"}

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Streamed: the core HttpClient decodes every body as JSON or text,
        # which would silently corrupt a binary workbook.
        with httpx.stream(
            "GET",
            url,
            params=params,
            headers=_headers(token),
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise ApiError(
                    f"Drive export returned {response.status_code}",
                    status_code=response.status_code,
                    body=_explain(response),
                )
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise ApiError(f"Drive export failed: {exc}") from exc
    return meta
