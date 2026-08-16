"""One HTTP client, with the timeouts and retries a CLI actually needs.

Replaces the ~23 hand-rolled ``urllib.request`` blocks that each re-implemented
headers, JSON encoding, error mapping and the Cloudflare workaround. Built on
httpx so timeouts, redirects and connection reuse come for free.

The Cloudflare profile matters: production (``api``/``back``) sits behind
Cloudflare, which rejects a bare Python user agent. The browser-shaped header
set below is what the automation scripts converged on.
"""

from __future__ import annotations

import os

import json
from collections.abc import Mapping
from typing import Any, Self

import httpx

from opscore.errors import (
    ApiError,
    AuthenticationError,
    NotFoundError,
    UpstreamTimeoutError,
)

DEFAULT_TIMEOUT = 30.0

WRITE_TIMEOUT = 180.0
"""Writes get their own ceiling.

Some writes fan out into queue work before answering — creating a lesson
has been observed taking over 30s while succeeding. A client that gives up
early reports a failure for a write that landed, and a naive retry then
duplicates it.
"""

BASE_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
}

# Browser-shaped headers, required for any call that traverses Cloudflare.
CLOUDFLARE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.6 Safari/605.1.15"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Origin": os.environ.get("OPSCORE_ORIGIN", ""),
    "Referer": os.environ.get("OPSCORE_REFERER", ""),
    "Priority": "u=3, i",
}


class HttpClient:
    """Thin JSON client with Fitco's auth and error conventions baked in.

    Usage::

        with HttpClient(base_url=settings.host("api"), token=token) as http:
            data = http.get("/lessons/lessons", params={"establishmentId": 2213})
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        token: str | None = None,
        cloudflare: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=self._build_headers(cloudflare, headers),
        )

    def _build_headers(self, cloudflare: bool, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = dict(BASE_HEADERS)
        if cloudflare:
            headers.update(CLOUDFLARE_HEADERS)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def set_token(self, token: str | None) -> None:
        """Attach (or clear) the bearer token for subsequent requests."""
        self.token = token
        if token:
            self._client.headers["Authorization"] = f"Bearer {token}"
        else:
            self._client.headers.pop("Authorization", None)

    # --- verbs --------------------------------------------------------------
    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json_body=json_body, **kwargs)

    def put(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, json_body=json_body, **kwargs)

    def patch(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self.request("PATCH", path, json_body=json_body, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Perform a request and return the decoded JSON body.

        Raises:
            AuthenticationError: on 401, and on a 403 whose body mentions a
                token. A bare 403 is *not* auth — Fitco answers 403 for domain
                errors too (see :meth:`_decode`) — and becomes an ApiError.
            NotFoundError: on 404.
            UpstreamTimeoutError: the call did not answer in time. A write that timed
                out may still have landed; check before retrying.
            ApiError: any other non-2xx response, or a transport failure.
        """
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._client.request(
                method,
                url,
                json=json_body,
                params=dict(params) if params else None,
                headers=dict(headers) if headers else None,
                timeout=timeout if timeout is not None else self._client.timeout,
            )
        except httpx.TimeoutException as exc:
            # Deliberately typed apart from a transport failure: a connection
            # refused says the request never ran, a timeout says nothing of the
            # sort. On a write those need different next steps.
            raise UpstreamTimeoutError(f"{method} {url} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"{method} {url} failed: {exc}") from exc

        return self._decode(response, method, url)

    @staticmethod
    def _decode(response: httpx.Response, method: str, url: str) -> Any:
        # 401 is always auth. 403 is not: Fitco answers 403 for domain errors
        # too (`INVALID_OBJECT_ID` from lessons delete-future, for one), and
        # reporting those as an expired session sends the reader hunting for a
        # credential problem that does not exist. Only call it auth when the
        # body says so.
        if response.status_code == 401 or (
            response.status_code == 403 and "TOKEN" in response.text.upper()
        ):
            raise AuthenticationError(
                f"{method} {url} rejected the token ({response.status_code})",
                detail=response.text[:500] or None,
                status_code=response.status_code,
            )
        if response.status_code == 404:
            raise NotFoundError(f"{method} {url} returned 404", detail=response.text[:500] or None)
        if response.status_code >= 400:
            raise ApiError(
                f"{method} {url} returned {response.status_code}",
                status_code=response.status_code,
                body=response.text[:1000] or None,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    # --- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
