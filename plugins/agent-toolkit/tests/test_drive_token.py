"""How agentctl drive gets a Drive-capable token."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from agentctl import drive


class _Gcloud:
    def __init__(self, scoped_ok: bool) -> None:
        self.scoped_ok = scoped_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if "--scopes" in argv:
            if self.scoped_ok:
                return subprocess.CompletedProcess(argv, 0, "scoped-token\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "Invalid scopes value")
        return subprocess.CompletedProcess(argv, 0, "plain-token\n", "")


def test_rule_a_service_account_asks_for_the_drive_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: a service-account token carries no Drive scope unless requested, so the
    first request names it — a plain token would be refused by Drive."""
    gcloud = _Gcloud(scoped_ok=True)
    monkeypatch.delenv(drive.TOKEN_ENV, raising=False)
    monkeypatch.setattr(drive.subprocess, "run", gcloud)
    assert drive.token() == "scoped-token"
    assert gcloud.calls[0][-2:] == ["--scopes", drive.DRIVE_SCOPE]
    assert len(gcloud.calls) == 1


def test_rule_a_user_login_falls_back_to_its_consented_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule: a user login cannot add a scope by flag (it can only narrow), so when the
    scoped request fails the plain token — which carries the consented scope — is used."""
    gcloud = _Gcloud(scoped_ok=False)
    monkeypatch.delenv(drive.TOKEN_ENV, raising=False)
    monkeypatch.setattr(drive.subprocess, "run", gcloud)
    assert drive.token() == "plain-token"
    assert [c[-1] for c in gcloud.calls] == [drive.DRIVE_SCOPE, "print-access-token"]
