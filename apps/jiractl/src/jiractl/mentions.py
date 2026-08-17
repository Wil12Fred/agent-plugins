"""Resolving people to ``accountId`` so a comment can actually tag them.

An ADF mention needs the account id, not a name. The scripts this replaces each
pasted a hardcoded id ("Kenia", "Ronaldo", Customer Success) with no way to look
up anyone else. Here a mention is resolved by name or email against
``GET /rest/api/3/user/search``, with the well-known recipients kept as aliases
so the common case stays a single word.
"""

from __future__ import annotations

from dataclasses import dataclass

from opscore.errors import NotFoundError, ValidationError

from jiractl.client import JiraClient

# Recipients this repo tags constantly. Everything else goes through search.
ALIASES: dict[str, tuple[str, str]] = {
    "cs": ("5b45231f978af72cc15b7a06", "@Customer Success"),
    "customer-success": ("5b45231f978af72cc15b7a06", "@Customer Success"),
}

ACCOUNT_ID_HINT = ":"


@dataclass(frozen=True)
class Person:
    """A resolved JIRA account."""

    account_id: str
    display_name: str

    @property
    def mention_text(self) -> str:
        return self.display_name if self.display_name.startswith("@") else f"@{self.display_name}"

    def as_dict(self) -> dict[str, str]:
        return {"account_id": self.account_id, "display_name": self.display_name}


def resolve(client: JiraClient, query: str) -> Person:
    """Resolve ``query`` (alias, account id, email or display name) to a person.

    Raises:
        NotFoundError: nobody matched.
        ValidationError: several people matched — name the person precisely.
    """
    normalized = query.strip()
    if not normalized:
        raise ValidationError("empty mention query")

    alias = ALIASES.get(normalized.lower())
    if alias:
        return Person(account_id=alias[0], display_name=alias[1])

    # Account ids look like `712020:09716950-...` or a 24-char hex string.
    if ACCOUNT_ID_HINT in normalized or (len(normalized) == 24 and normalized.isalnum()):
        return Person(account_id=normalized, display_name=normalized)

    matches = client.search_users(normalized)
    active = [m for m in matches if m.get("active", True)]
    if not active:
        raise NotFoundError(f"no JIRA user matches {query!r}")
    if len(active) > 1:
        names = ", ".join(str(m.get("displayName")) for m in active[:5])
        raise ValidationError(
            f"{query!r} matches {len(active)} users: {names}",
            detail="use the full display name, the email, or the accountId",
        )

    found = active[0]
    return Person(
        account_id=str(found.get("accountId", "")),
        display_name=str(found.get("displayName", normalized)),
    )


def resolve_all(client: JiraClient, queries: list[str]) -> list[Person]:
    """Resolve several mentions, preserving order."""
    return [resolve(client, query) for query in queries]
