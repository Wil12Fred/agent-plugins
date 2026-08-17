"""Where the JIRA site, the account and the token come from.

Three values, all from the environment, and none with a default. A JIRA client
that guesses a site is a client that reports "issue not found" for a ticket that
exists — the worst answer available, because it looks like a real one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from opscore.errors import ConfigError
from opscore.secrets import resolve

KEYRING_TOKEN = ("atlassian", "api-token")


@dataclass(frozen=True)
class JiraConfig:
    site: str
    """Host only, e.g. `acme.atlassian.net` — no scheme."""

    email: str
    """The account the token belongs to. Basic auth pairs the two."""

    token: str

    @property
    def base_url(self) -> str:
        return f"https://{self.site}"

    def browse(self, key: str) -> str:
        """The human URL for an issue, which is not the API URL."""
        return f"{self.base_url}/browse/{key}"


def load() -> JiraConfig:
    """Resolve the configuration, naming what is missing rather than guessing."""
    site = os.environ.get("JIRA_SITE", "").strip().removeprefix("https://").rstrip("/")
    if not site:
        raise ConfigError(
            "no JIRA site: set JIRA_SITE",
            detail="the host only, for example acme.atlassian.net",
        )
    email = os.environ.get("JIRA_EMAIL") or os.environ.get("ATLASSIAN_EMAIL", "")
    if not email:
        raise ConfigError(
            "no account email: set JIRA_EMAIL",
            detail="the API token is tied to an account, and Basic auth pairs the two",
        )
    token = resolve(env_var="JIRA_TOKEN", keyring=KEYRING_TOKEN, required=False) or resolve(
        env_var="ATLASSIAN_TOKEN", keyring=KEYRING_TOKEN, required=False
    )
    if not token:
        raise ConfigError(
            "no API token: set JIRA_TOKEN",
            detail=f"or store it in the keyring as {KEYRING_TOKEN[0]}/{KEYRING_TOKEN[1]}",
        )
    return JiraConfig(site=site, email=email, token=token)
