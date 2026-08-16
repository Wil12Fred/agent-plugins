"""Google credentials — two scopes, two completely different stories.

**Drive (`drive.readonly`) is a *sensitive* scope.** It can be added to the
user's ordinary `gcloud` login, so `gcloud auth print-access-token` is enough
once the consent has happened. One time per workstation::

    gcloud auth login --enable-gdrive-access --update-adc

That *adds* the Drive scope to the existing login rather than replacing it, and
after it tokens are minted indefinitely with no further prompts. The scope
cannot be added non-interactively: ``gcloud auth print-access-token
--scopes=…drive.readonly`` fails with "Invalid scopes value" because it may only
*narrow* to already-granted scopes, never grant a new one.

**Gmail (`gmail.readonly`) is a *restricted* scope, and the same trick does not
work.** Google refuses the shared `gcloud` OAuth client for restricted scopes —
confirmed on a Workspace domain, where ``gcloud auth application-default login
--scopes=…gmail.readonly`` returns *acceso bloqueado*. So ADC can never carry a
Gmail scope here, no matter how it is invoked.

The supported path is a **dedicated OAuth client whose consent screen is
User Type = Internal**: an org-internal app may use restricted scopes for its
own users without Google verification. Console prerequisites, once, by hand:

1. a project in your organisation → enable the Gmail API;
2. OAuth consent screen → **User Type = Internal**, add `…/auth/gmail.readonly`;
3. Credentials → OAuth client ID → **Desktop app** → download `client_secret_*.json`.

Then :func:`run_consent` performs the loopback flow once and stores the refresh
token in the OS keyring; :func:`mint_access_token` turns it into short-lived
access tokens from then on.

Nothing here ever prints a token, a client secret or a refresh token — except
``gpull auth token``, whose entire purpose is to emit one for piping.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import shutil
import subprocess
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx
from opscore.errors import ApiError, ConfigError
from opscore.secrets import (
    from_env,
    from_keyring,
    redact,
    warn_if_passed_on_the_command_line,
)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKENINFO_URI = "https://www.googleapis.com/oauth2/v1/tokeninfo"

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_READ_SCOPES = (
    GMAIL_SCOPE,
    "https://www.googleapis.com/auth/gmail.modify",
    "https://mail.google.com/",
)
DRIVE_SCOPE_MARKER = "drive"

KEYRING_OAUTH = ("gmail-oauth", "gpull")
"""Where the dedicated client's refresh token lives (a JSON blob)."""

KEYRING_RAW_TOKEN = ("gmail-readonly", "gpull")
"""Escape hatch: a raw access token someone pasted in by hand."""

CONSENT_TIMEOUT_SECONDS = 300
HTTP_TIMEOUT = 60.0

SETUP_HINT = (
    "gmail.readonly is a RESTRICTED scope: the shared gcloud OAuth client is refused for it "
    "(confirmed for example.com), so the ADC path does not work. Create a Desktop OAuth client "
    "in a project of the example.com org with consent screen User Type = Internal, then run: "
    "gpull auth consent --client-secret <client_secret.json>"
)


@dataclass(frozen=True)
class StoredCredentials:
    """The dedicated Gmail client, as kept in the keyring."""

    client_id: str
    client_secret: str
    refresh_token: str
    scope: str = GMAIL_SCOPE

    def redacted(self) -> dict[str, object]:
        """Describe the stored state without revealing any of it."""
        return {
            # `redact`, not a local slice: the consolidation exists so there
            # is one rule, and an 18-character slice ignores its length floors.
            "client_id": redact(self.client_id),
            "refresh_token": bool(self.refresh_token),
            "scope": self.scope,
        }


def load_credentials() -> StoredCredentials | None:
    """Read the stored Gmail OAuth client from the keyring, if consent has run."""
    blob = from_keyring(*KEYRING_OAUTH)
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not data.get("refresh_token"):
        return None
    return StoredCredentials(
        client_id=str(data.get("client_id", "")),
        client_secret=str(data.get("client_secret", "")),
        refresh_token=str(data["refresh_token"]),
        scope=str(data.get("scope", GMAIL_SCOPE)),
    )


def store_credentials(creds: StoredCredentials) -> None:
    """Write the client + refresh token into the OS keyring (never to disk)."""
    if shutil.which("secret-tool") is None:
        raise ConfigError("secret-tool not found: install libsecret-tools to store the credential")
    subprocess.run(
        [
            "secret-tool",
            "store",
            "--label=Gmail OAuth (gpull)",
            "service",
            KEYRING_OAUTH[0],
            "account",
            KEYRING_OAUTH[1],
        ],
        input=json.dumps(
            {
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "refresh_token": creds.refresh_token,
                "scope": creds.scope,
            }
        ),
        text=True,
        timeout=15,
        check=True,
    )


def read_client_secret(path: Path) -> tuple[str, str]:
    """Pull ``(client_id, client_secret)`` out of a downloaded client JSON."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read the client-secret JSON: {exc}") from exc
    node = document.get("installed") or document.get("web") or {}
    client_id, client_secret = node.get("client_id"), node.get("client_secret")
    if not client_id or not client_secret:
        raise ConfigError(
            "client-secret JSON has no installed/web client_id + client_secret",
            detail="download it from Credentials → OAuth client ID → Desktop app",
        )
    return str(client_id), str(client_secret)


def _post_form(form: dict[str, str]) -> dict[str, object]:
    try:
        response = httpx.post(TOKEN_URI, data=form, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ApiError(f"token endpoint unreachable: {exc}") from exc
    if response.status_code >= 400:
        raise ApiError(
            f"Google token endpoint returned {response.status_code}",
            status_code=response.status_code,
            body=response.text[:300],
        )
    return dict(response.json())


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    """Catches the single redirect Google makes back to the loopback address."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeHandler.code = next(iter(params.get("code", [])), None)
        _CodeHandler.error = next(iter(params.get("error", [])), None)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "Gmail authorization received. You can close this tab."
            if _CodeHandler.code
            else f"Authorization failed: {_CodeHandler.error}"
        )
        self.wfile.write(f"<html><body><h3>{message}</h3></body></html>".encode())

    def log_message(self, *_: object) -> None:
        return


def run_consent(client_secret_path: Path, *, scope: str = GMAIL_SCOPE, port: int = 8765) -> str:
    """Run the installed-app loopback flow once and store the refresh token.

    Interactive by nature: it opens a browser and the user consents as
    ``wilber.cutire@example.com``. Nothing on the organisation's side is mutated — the
    only write is the credential in the local OS keyring.

    Returns:
        The account-facing summary line (never the token).
    """
    client_id, client_secret = read_client_secret(client_secret_path)
    redirect_uri = f"http://127.0.0.1:{port}"
    auth_url = f"{AUTH_URI}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            # Without prompt=consent a re-authorisation returns no refresh
            # token at all, and the stored credential would be useless.
            "prompt": "consent",
        }
    )

    _CodeHandler.code = _CodeHandler.error = None
    server = http.server.HTTPServer(("127.0.0.1", port), _CodeHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    # A headless or restricted box has no browser to open; the URL is printed
    # in the error path instead, so failing to launch one is not fatal.
    with contextlib.suppress(Exception):
        webbrowser.open(auth_url)
    thread.join(timeout=CONSENT_TIMEOUT_SECONDS)
    server.server_close()

    if _CodeHandler.error:
        raise ApiError(f"consent failed: {_CodeHandler.error}")
    if not _CodeHandler.code:
        raise ApiError(f"no authorization code captured; open it manually:\n{auth_url}")

    token = _post_form(
        {
            "code": _CodeHandler.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise ApiError(
            "Google returned no refresh_token",
            body="revoke the prior grant for this client and retry",
        )
    store_credentials(
        StoredCredentials(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=str(refresh_token),
            scope=scope,
        )
    )
    return f"refresh token stored in the keyring (service={KEYRING_OAUTH[0]})"


def mint_access_token() -> str | None:
    """Exchange the stored refresh token for a short-lived access token."""
    creds = load_credentials()
    if creds is None:
        return None
    token = _post_form(
        {
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        }
    )
    value = token.get("access_token")
    return str(value) if value else None


def gcloud_token() -> str | None:
    """Mint a token from the user's gcloud login (Drive path; never Gmail)."""
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        return None
    try:
        completed = subprocess.run(
            [gcloud, "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def token_scopes(token: str) -> list[str]:
    """Ask Google which scopes a token carries. Never echoes the token."""
    try:
        response = httpx.get(TOKENINFO_URI, params={"access_token": token}, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError:
        return []
    if response.status_code >= 400:
        return []
    return str(response.json().get("scope", "")).split()


def has_gmail_scope(token: str) -> bool:
    """Whether a token can read Gmail.

    An empty tokeninfo answer is treated as "yes": tokeninfo failing is not
    evidence the token is bad, and letting the real Gmail call return 401/403
    gives a far better message than a guess does.
    """
    scopes = token_scopes(token)
    if not scopes:
        return True
    return any(scope in scopes for scope in GMAIL_READ_SCOPES)


def has_drive_scope(token: str) -> bool:
    """Whether a token can read Drive (same "unknown means yes" rule)."""
    scopes = token_scopes(token)
    if not scopes:
        return True
    return any(DRIVE_SCOPE_MARKER in scope for scope in scopes)


def resolve_gmail_token(explicit: str | None = None) -> str:
    """Find a token that can read Gmail, in order of how reliable the source is.

    1. ``--token``;
    2. ``GMAIL_ACCESS_TOKEN``;
    3. keyring ``gmail-readonly/gpull`` (a raw access token pasted in by hand);
    4. the dedicated Internal OAuth client's refresh token — **the path that
       actually works**.

    ADC is deliberately not in this list: it cannot carry a restricted scope
    here, so falling back to it only produces a confusing 403 from Gmail.

    Raises:
        ConfigError: nothing resolved, or what resolved has no Gmail scope.
    """
    warn_if_passed_on_the_command_line(flag="--token", value=explicit, env_var="GMAIL_ACCESS_TOKEN")
    token = explicit or from_env("GMAIL_ACCESS_TOKEN") or from_keyring(*KEYRING_RAW_TOKEN)
    if not token:
        token = mint_access_token()
    if not token:
        raise ConfigError("no Gmail access token available", detail=SETUP_HINT)
    if not has_gmail_scope(token):
        raise ConfigError("the resolved token carries no Gmail read scope", detail=SETUP_HINT)
    return token.strip()


def resolve_drive_token(explicit: str | None = None) -> str:
    """Find a token that can read Drive: ``--token``, env, then gcloud.

    Raises:
        ConfigError: no token, or the gcloud login never consented to Drive.
    """
    warn_if_passed_on_the_command_line(
        flag="--token", value=explicit, env_var="GOOGLE_ACCESS_TOKEN"
    )
    token = explicit or from_env("GOOGLE_ACCESS_TOKEN") or gcloud_token()
    if not token:
        raise ConfigError(
            "no Google access token available",
            detail="run: gcloud auth login --enable-gdrive-access --update-adc",
        )
    token = token.strip()
    if not has_drive_scope(token):
        raise ConfigError(
            "the gcloud login has no Drive scope",
            detail=(
                "the default login grants only cloud-platform/compute/userinfo; add Drive once "
                "with: gcloud auth login --enable-gdrive-access --update-adc "
                "(it cannot be added non-interactively)"
            ),
        )
    return token
