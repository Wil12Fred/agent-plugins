"""JIRA REST v3 client.

Auth is HTTP Basic with ``<atlassian email>:<ATLASSIAN_TOKEN>``. The token is
resolved through :mod:`opscore.secrets` and never rendered.

The repo's convention (``scripts/jira/README.md``) was: ACLI for plain text,
REST + ADF for anything with formatting. Since every comment worth posting has
formatting, this client always speaks ADF.
"""

from __future__ import annotations

import base64
from typing import Any

from opscore.env import load_env_file
from opscore.errors import NotFoundError
from opscore.http import HttpClient

from jiractl import config

API_V3 = "/rest/api/3"


class JiraClient:
    """Thin wrapper over the JIRA Cloud REST API."""

    def __init__(self, cfg: config.JiraConfig | None = None) -> None:
        # `load_env_file()` first: it exports the .env into os.environ, which is
        # where the configuration and the keyring resolver both look.
        load_env_file()
        self.config = cfg or config.load()
        basic = base64.b64encode(f"{self.config.email}:{self.config.token}".encode()).decode(
            "ascii"
        )
        self.base_url = self.config.base_url
        self._auth_header = f"Basic {basic}"
        self._http = HttpClient(
            base_url=self.base_url,
            headers={"Authorization": self._auth_header},
        )

    @property
    def auth_header(self) -> str:
        """The ``Authorization`` header value, for calls that need raw HTTP.

        Attachment upload is multipart and the media-UUID lookup must not
        follow redirects, so neither can go through the JSON client.
        """
        return self._auth_header

    # --- issues -------------------------------------------------------------
    def get_issue(self, key: str, *, fields: str | None = None) -> dict[str, Any]:
        params = {"fields": fields} if fields else None
        result = self._http.get(f"{API_V3}/issue/{key}", params=params)
        if not isinstance(result, dict):
            raise NotFoundError(f"unexpected response for {key}")
        return result

    def search(
        self, jql: str, *, limit: int = 50, fields: str = "summary,status,assignee"
    ) -> list[dict[str, Any]]:
        result = self._http.get(
            f"{API_V3}/search/jql",
            params={"jql": jql, "maxResults": limit, "fields": fields},
        )
        issues = result.get("issues", []) if isinstance(result, dict) else []
        return list(issues)

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        result = self._http.post(f"{API_V3}/issue", json_body={"fields": fields})
        return result if isinstance(result, dict) else {}

    def update_issue(self, key: str, fields: dict[str, Any]) -> None:
        self._http.put(f"{API_V3}/issue/{key}", json_body={"fields": fields})

    # --- comments -----------------------------------------------------------
    def add_comment(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        """Post an ADF comment. ``body`` must be a complete ADF document."""
        result = self._http.post(f"{API_V3}/issue/{key}/comment", json_body={"body": body})
        return result if isinstance(result, dict) else {}

    def list_comments(self, key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        result = self._http.get(f"{API_V3}/issue/{key}/comment", params={"maxResults": limit})
        comments = result.get("comments", []) if isinstance(result, dict) else []
        return list(comments)

    # --- workflow -----------------------------------------------------------
    def transitions(self, key: str) -> list[dict[str, Any]]:
        result = self._http.get(f"{API_V3}/issue/{key}/transitions")
        transitions = result.get("transitions", []) if isinstance(result, dict) else []
        return list(transitions)

    def transition(self, key: str, transition_id: str) -> None:
        self._http.post(
            f"{API_V3}/issue/{key}/transitions",
            json_body={"transition": {"id": transition_id}},
        )

    # --- users --------------------------------------------------------------
    def search_users(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        result = self._http.get(
            f"{API_V3}/user/search", params={"query": query, "maxResults": limit}
        )
        return list(result) if isinstance(result, list) else []

    # --- projects -----------------------------------------------------------
    def projects(self) -> list[dict[str, Any]]:
        result = self._http.get(f"{API_V3}/project/search", params={"maxResults": 100})
        values = result.get("values", []) if isinstance(result, dict) else []
        return list(values)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
