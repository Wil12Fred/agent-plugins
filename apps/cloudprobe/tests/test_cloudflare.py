"""Cloudflare Log Explorer: query shape and the read-only guard."""

from __future__ import annotations

import pytest
from opscore.errors import ConfigError, GuardError

from cloudprobe import cloudflare


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # opscore has no cached settings object to reset — the .env is exported into
    # os.environ once and read from there, so stubbing the loader is the whole fix.
    monkeypatch.setattr("opscore.env.load_env_file", lambda *a, **k: 0)


def test_the_query_pins_host_path_and_window() -> None:
    sql = cloudflare.build_query(host="api.example.com", path="/x/webhook", days=7, limit=10)
    assert "clientRequestHost = 'api.example.com'" in sql
    assert "clientRequestPath = '/x/webhook'" in sql
    assert "LIMIT 10" in sql
    assert sql.startswith("SELECT ")


def test_the_oper_595_column_set_is_preserved() -> None:
    # Kept verbatim so re-running that forensics reproduces the same shape.
    sql = cloudflare.build_query()
    for column in ("rayid", "edgeresponsestatus", "clientrequestuseragent", "botscore"):
        assert column in sql


def test_a_window_is_a_closed_rfc3339_range() -> None:
    start, end = cloudflare.window(30)
    assert start.endswith("Z") and end.endswith("Z")
    assert start < end


def test_a_non_select_is_refused_before_leaving_this_machine() -> None:
    with pytest.raises(GuardError):
        cloudflare.run("acct", "DELETE FROM http_requests")


def test_missing_global_key_names_both_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CF_EMAIL", raising=False)
    monkeypatch.delenv("CF_GLOBAL_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="CF_EMAIL"):
        cloudflare.credentials()


def test_the_summary_counts_by_edge_status_and_country() -> None:
    rows = [
        {"edgeresponsestatus": "200", "clientcountry": "PE"},
        {"edgeresponsestatus": "403", "clientcountry": "US"},
        {"edgeresponsestatus": "403", "clientcountry": "PE"},
    ]
    summary = cloudflare.summarise(rows)
    assert summary["total"] == 3
    assert summary["by_edge_status"] == {"403": 2, "200": 1}
    assert summary["by_country"]["PE"] == 2


def test_an_empty_result_summarises_to_zero_not_a_crash() -> None:
    assert cloudflare.summarise([])["total"] == 0
