"""Parsing and safety rules for the Google helpers — no network, no keyring."""

from __future__ import annotations

import base64

import pytest
from opscore.errors import ValidationError

from gpull.drive import EXPORT_MIME, DriveFile, ExportFormat, extract_file_id
from gpull.gmail import decode_attachment, iter_attachments, safe_filename
from gpull.oauth import GMAIL_READ_SCOPES, GMAIL_SCOPE, StoredCredentials


def test_attachments_are_found_at_every_nesting_level() -> None:
    # multipart/mixed → multipart/alternative → part is Gmail's normal shape,
    # and a flat walk misses everything below the first level.
    payload = {
        "filename": "",
        "body": {},
        "parts": [
            {
                "filename": "top.pdf",
                "mimeType": "application/pdf",
                "body": {"attachmentId": "a1", "size": 10},
            },
            {
                "filename": "",
                "body": {},
                "parts": [
                    {
                        "filename": "deep.png",
                        "mimeType": "image/png",
                        "body": {"attachmentId": "a2", "size": 20},
                    },
                ],
            },
        ],
    }
    found = {a.attachment_id: a.filename for a in iter_attachments(payload)}
    assert found == {"a1": "top.pdf", "a2": "deep.png"}


def test_inline_parts_without_an_attachment_id_are_not_attachments() -> None:
    payload = {"parts": [{"filename": "", "mimeType": "text/html", "body": {"size": 900}}]}
    assert list(iter_attachments(payload)) == []


def test_gmail_base64url_is_decoded_despite_missing_padding() -> None:
    raw = b"\xff\xd8\xff\xe0 binary jpeg header"
    unpadded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert decode_attachment(unpadded) == raw


def test_a_filename_from_the_wire_cannot_escape_the_output_directory() -> None:
    assert safe_filename("../../etc/passwd", "fallback") == "passwd"
    assert safe_filename("/absolute/evil.sh", "fallback") == "evil.sh"
    assert safe_filename("", "fallback") == "fallback"
    assert safe_filename("informe final.pdf", "fallback") == "informe final.pdf"
    assert safe_filename("rep;ort$(id).csv", "fallback") == "rep_ort__id_.csv"


def test_a_pasted_sheets_url_yields_its_file_id() -> None:
    url = "https://docs.google.com/spreadsheets/d/1AbC_def-GHI23456789jklmnop/edit#gid=0"
    assert extract_file_id(url) == "1AbC_def-GHI23456789jklmnop"


def test_a_bare_id_passes_through_and_nonsense_is_refused() -> None:
    assert extract_file_id("  1AbC_def-GHI23456789jklmnop ") == "1AbC_def-GHI23456789jklmnop"
    with pytest.raises(ValidationError):
        extract_file_id("https://example.com/not/a/drive/link")


def test_only_a_native_sheet_needs_exporting() -> None:
    native = DriveFile("id", "Migración", "application/vnd.google-apps.spreadsheet")
    uploaded = DriveFile(
        "id",
        "Migración.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert native.is_native_sheet
    assert not uploaded.is_native_sheet


def test_xlsx_export_keeps_every_tab_csv_does_not() -> None:
    # The migrator reads worksheets[0]; exporting to CSV silently discards the
    # other tabs, so xlsx is the default for a reason.
    assert "spreadsheetml.sheet" in EXPORT_MIME[ExportFormat.XLSX]
    assert EXPORT_MIME[ExportFormat.CSV] == "text/csv"


def test_stored_credentials_never_render_the_secret() -> None:
    creds = StoredCredentials(
        client_id="1234567890-abcdefghijklmnop.apps.googleusercontent.com",
        client_secret="GOCSPX-super-secret",
        refresh_token="1//0eXXXXXXXXXXXXXXXX",
    )
    rendered = str(creds.redacted())
    assert "GOCSPX" not in rendered
    assert "1//0e" not in rendered
    assert creds.redacted()["refresh_token"] is True


def test_the_restricted_gmail_scope_is_the_default_and_the_broader_ones_count() -> None:
    assert GMAIL_SCOPE in GMAIL_READ_SCOPES
    assert "https://mail.google.com/" in GMAIL_READ_SCOPES
