"""The items ledger: an item handed to the alerts session is recorded, and handed again
only when nobody took it.

Observed before it existed: every restart's catch-up re-delivered alerts that were already
delivered and handled; two test alerts arrived three times. Observed after it existed (the
operator's rule, 2026-11-25): a worker stopped right after the hand-over left a question
dispatched, unanswered and untouched, and the catch-up never sent it again because "known"
had been taken to mean "handled". These tests pin both: a live redelivery is never
dispatched twice; the catch-up re-sends an empty-threaded item only the bridge ever wrote.
Also the CLI the session uses to record what it did, and thread replies being routed by the
ledger (alert and question threads alike) to whoever handles the item.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import BOT_USER, HUMAN, make_config
from opscore.cli import run
from opscore.errors import ConfigError, NotFoundError, ValidationError

from slackbridge import alerts, claude, cli, items
from slackbridge.config import SlackConfig
from slackbridge.listeners import messages

ALERTS = "C0ALERTS"
QA = "C0QUESTIONS"
REPORTER = "U0REPORTER"
TRIGGER = "URGENT ALERT"
ALERT_TEXT = "URGENT ALERT\nTicket: ABC-123\nMotivo: no carga"
LOG = logging.getLogger("test-items")
TS = "1795564509.630819"
TS2 = "1795564600.000100"


def cfg(**overrides: object) -> SlackConfig:
    base: dict[str, object] = {
        "alerts_channel_ids": frozenset({ALERTS, QA}),
        "alerts_trigger": TRIGGER,
        "alerts_channel_triggers": {QA: alerts.ANY},
        "alerts_session_name": "triage",
        "alerts_ignore_user_ids": frozenset({HUMAN}),
        "workspace_url": "https://ws.example.test",
    }
    base.update(overrides)
    return make_config(**base)


def top(channel: str, ts: str, text: str, user: str = REPORTER) -> dict[str, Any]:
    return {"channel": channel, "ts": ts, "user": user, "text": text}


def reply(channel: str, ts: str, thread_ts: str, text: str, user: str = REPORTER) -> dict[str, Any]:
    return {"channel": channel, "ts": ts, "thread_ts": thread_ts, "user": user, "text": text}


@pytest.fixture(autouse=True)
def _fresh_dedup() -> None:
    messages._processed.clear()


@pytest.fixture
def store(tmp_path: Path) -> alerts.AlertStore:
    return alerts.AlertStore(tmp_path / "alerts.json")


def deliver(
    event: dict[str, Any], store: alerts.AlertStore, config: SlackConfig | None = None
) -> list[alerts.AlertEvent]:
    """One live delivery through ``handle``, dispatch captured inline."""
    sent: list[alerts.AlertEvent] = []
    alerts.handle(event, config or cfg(), LOG, store=store, run=lambda fn, ev, *a: sent.append(ev))
    return sent


def restart(tmp_path: Path, name: str = "alerts.json") -> alerts.AlertStore:
    """A new process: a fresh store object and an empty in-memory dedup."""
    messages._processed.clear()
    return alerts.AlertStore(tmp_path / name)


# --- recorded on dispatch -------------------------------------------------------
def test_a_dispatched_alert_is_written_to_the_ledger(store: alerts.AlertStore) -> None:
    """Rule: routing a top-level item records it with status `dispatched`."""
    assert len(deliver(top(ALERTS, TS, ALERT_TEXT), store)) == 1
    entry = items.ItemLedger().get(f"{ALERTS}:{TS}")
    assert entry is not None
    assert (entry["kind"], entry["ticket"], entry["status"]) == ("alert", "ABC-123", "dispatched")
    assert entry["permalink"] == f"https://ws.example.test/archives/{ALERTS}/p1795564509630819"
    assert [h["status"] for h in entry["history"]] == ["dispatched"]
    assert entry["first_dispatched_at"] and entry["handler"] is None


def test_a_dispatched_question_is_written_as_a_question(store: alerts.AlertStore) -> None:
    deliver(top(QA, TS, "¿cómo exporto?"), store)
    entry = items.ItemLedger().get(f"{QA}:{TS}")
    assert entry is not None and entry["kind"] == "question"


# --- never twice ----------------------------------------------------------------
def test_a_redelivery_after_a_restart_is_not_dispatched_again(tmp_path: Path) -> None:
    """Rule: a known item is never dispatched again — the in-memory dedup does not survive
    a restart, the ledger does."""
    assert len(deliver(top(ALERTS, TS, ALERT_TEXT), restart(tmp_path))) == 1
    assert deliver(top(ALERTS, TS, ALERT_TEXT), restart(tmp_path, "other.json")) == []


@pytest.mark.parametrize("status", ["dispatched", "done", "anything at all"])
def test_a_known_item_is_skipped_whatever_its_status(tmp_path: Path, status: str) -> None:
    deliver(top(QA, TS, "duda"), restart(tmp_path))
    items.ItemLedger().set_status(f"{QA}:{TS}", status)
    assert deliver(top(QA, TS, "duda"), restart(tmp_path, "other.json")) == []


class _History:
    def __init__(self, by_channel: dict[str, list[dict[str, Any]]]) -> None:
        self.by_channel = by_channel

    def conversations_history(self, channel: str, **_: Any) -> dict[str, Any]:
        return {"messages": self.by_channel.get(channel, [])}


def catch_up(store: alerts.AlertStore, client: _History) -> list[alerts.AlertEvent]:
    sent: list[alerts.AlertEvent] = []
    alerts.backfill(
        client, cfg(), LOG, store=store, run=lambda fn, ev, *a: sent.append(ev), now=1795564700.0
    )
    return sent


def test_catch_up_resends_a_dispatched_but_untouched_item_whose_thread_is_empty(
    tmp_path: Path,
) -> None:
    """Rule (operator, 2026-11-25): "known" is not "handled". An item the bridge dispatched,
    that nobody marked and whose thread is still empty, is sent again on every start."""
    client = _History({QA: [{"ts": TS, "user": REPORTER, "text": "duda"}]})
    assert len(catch_up(restart(tmp_path), client)) == 1
    assert len(catch_up(restart(tmp_path, "second.json"), client)) == 1
    entry = items.ItemLedger().get(f"{QA}:{TS}")
    assert entry is not None and items.untouched(entry)
    assert [h["by"] for h in entry["history"]] == [items.BRIDGE, items.BRIDGE]


def test_catch_up_resends_an_item_whose_hand_over_failed(tmp_path: Path) -> None:
    client = _History({ALERTS: [{"ts": TS, "user": REPORTER, "text": ALERT_TEXT}]})
    catch_up(restart(tmp_path), client)
    items.ItemLedger().set_status(f"{ALERTS}:{TS}", items.DISPATCH_ERROR, by=items.BRIDGE)
    assert len(catch_up(restart(tmp_path, "second.json"), client)) == 1


@pytest.mark.parametrize(
    ("status", "handler"),
    [
        ("in-progress", None),
        ("done", None),
        ("skipped", None),
        ("taken-by-other", None),
        ("En revisión", None),  # a tracker's own status name
        (None, "subagent-1"),  # only a handler recorded
    ],
)
def test_catch_up_never_resends_an_item_the_worker_touched(
    tmp_path: Path, status: str | None, handler: str | None
) -> None:
    """Rule: an item whose ledger entry the worker wrote (status, or just a handler) was
    taken; re-sending it would answer twice."""
    client = _History({ALERTS: [{"ts": TS, "user": REPORTER, "text": ALERT_TEXT}]})
    catch_up(restart(tmp_path), client)
    items.ItemLedger().set_status(f"{ALERTS}:{TS}", status, handler=handler, by="worker")
    assert catch_up(restart(tmp_path, "second.json"), client) == []


def test_catch_up_never_resends_an_untouched_item_whose_thread_has_a_reply(
    tmp_path: Path,
) -> None:
    """Rule: any reply in the thread means somebody answered; untouched or not, not re-sent."""
    empty = _History({QA: [{"ts": TS, "user": REPORTER, "text": "duda"}]})
    catch_up(restart(tmp_path), empty)
    answered = _History({QA: [{"ts": TS, "user": REPORTER, "text": "duda", "reply_count": 1}]})
    assert catch_up(restart(tmp_path, "second.json"), answered) == []


def test_catch_up_still_dispatches_an_unknown_item(store: alerts.AlertStore) -> None:
    client = _History({ALERTS: [{"ts": TS, "user": REPORTER, "text": ALERT_TEXT}]})
    assert [e.ticket for e in catch_up(store, client)] == ["ABC-123"]


# --- migration ----------------------------------------------------------------
def test_items_in_the_old_tracked_state_count_as_known(tmp_path: Path) -> None:
    """Rule: an item the old state tracked is known — a live redelivery is not dispatched —
    but it is untouched, so the catch-up re-sends it while its thread is empty."""
    (tmp_path / "alerts.json").write_text(json.dumps({"alerts": {TS: {"ticket": "ABC-123"}}}))
    store = alerts.AlertStore(tmp_path / "alerts.json")
    assert deliver(top(ALERTS, TS, ALERT_TEXT), store) == []
    client = _History({ALERTS: [{"ts": TS, "user": REPORTER, "text": ALERT_TEXT}]})
    assert len(catch_up(store, client)) == 1
    entry = items.ItemLedger().get(f"{ALERTS}:{TS}")
    assert entry is not None and entry["history"][0]["by"] == "migration"
    assert entry["history"][0]["status"] == items.DISPATCHED and items.untouched(entry)


def test_a_seeded_item_counts_as_known_too(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"alerts": {TS: {"ticket": None}}}))
    store = alerts.AlertStore(tmp_path / "alerts.json", seed)
    assert deliver(top(QA, TS, "duda"), store) == []


# --- thread replies ---------------------------------------------------------------
def test_replies_in_a_tracked_thread_are_still_routed_and_create_no_entry(
    store: alerts.AlertStore,
) -> None:
    deliver(top(ALERTS, TS, ALERT_TEXT), store)
    sent = deliver(reply(ALERTS, TS2, TS, "sigue fallando"), store)
    assert [e.kind for e in sent] == ["THREAD_REPLY"]
    assert [r["key"] for r in items.ItemLedger().all()] == [f"{ALERTS}:{TS}"]


def test_a_reply_in_an_open_ledger_question_thread_is_routed(tmp_path: Path) -> None:
    """Rule: the ledger alone routes replies — a question thread the tracked state no longer
    follows (another process, expired, pruned) still reaches the session while open."""
    deliver(top(QA, TS, "duda"), restart(tmp_path))
    items.ItemLedger().set_status(f"{QA}:{TS}", "in-progress")
    sent = deliver(reply(QA, TS2, TS, "¿algo?"), restart(tmp_path, "untracked.json"))
    assert [(e.kind, e.key, e.question) for e in sent] == [("THREAD_REPLY", f"{QA}:{TS}", True)]


@pytest.mark.parametrize("status", ["done", "skipped", "taken-by-other", "DONE"])
def test_a_reply_in_a_closed_ledger_thread_is_not_routed(tmp_path: Path, status: str) -> None:
    deliver(top(QA, TS, "duda"), restart(tmp_path))
    items.ItemLedger().set_status(f"{QA}:{TS}", status)
    assert deliver(reply(QA, TS2, TS, "gracias"), restart(tmp_path, "untracked.json")) == []


def test_the_tracker_closing_statuses_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliver(top(ALERTS, TS, ALERT_TEXT), restart(tmp_path))
    items.ItemLedger().set_status(f"{ALERTS}:{TS}", "QA - Released")
    assert len(deliver(reply(ALERTS, TS2, TS, "x"), restart(tmp_path, "u1.json"))) == 1
    monkeypatch.setenv("SLACKBRIDGE_TERMINAL_STATUSES", "QA - Released, Closed - Done")
    assert deliver(reply(ALERTS, "1795564601.0", TS, "x"), restart(tmp_path, "u2.json")) == []


@pytest.mark.parametrize("user", [HUMAN, BOT_USER])
def test_ledger_routed_replies_exclude_ignored_users_and_the_bridge(
    tmp_path: Path, user: str
) -> None:
    deliver(top(QA, TS, "duda"), restart(tmp_path))
    assert deliver(reply(QA, TS2, TS, "x", user=user), restart(tmp_path, "u.json")) == []


def test_a_tracked_thread_is_routed_even_when_its_item_is_closed(
    store: alerts.AlertStore,
) -> None:
    """Rule: today's tracked-thread routing is kept — the ledger adds threads, removes none."""
    deliver(top(ALERTS, TS, ALERT_TEXT), store)
    items.ItemLedger().set_status(f"{ALERTS}:{TS}", "done")
    assert [e.kind for e in deliver(reply(ALERTS, TS2, TS, "reabrir"), store)] == ["THREAD_REPLY"]


def test_tracked_and_ledger_together_route_one_event(store: alerts.AlertStore) -> None:
    """Rule: the two sources are a union — a reply known to both is still one dispatch."""
    deliver(top(QA, TS, "duda"), store)
    assert len(deliver(reply(QA, TS2, TS, "más info"), store)) == 1


def test_an_unknown_thread_is_still_ignored(store: alerts.AlertStore) -> None:
    assert deliver(reply(QA, TS2, TS, "en otro hilo"), store) == []


# --- the prompt -------------------------------------------------------------------
def test_the_prompt_carries_the_key_and_the_update_command(
    store: alerts.AlertStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACKBRIDGE_BIN", "/opt/venv/bin/slackbridge")
    config = cfg()
    alert = alerts.classify(top(ALERTS, TS, ALERT_TEXT), config, store)
    question = alerts.classify(top(QA, TS2, "duda"), config, store)
    assert alert is not None and question is not None
    a, q = alerts.build_prompt(alert, config), alerts.build_prompt(question, config)
    assert f"Item: {ALERTS}:{TS}" in a
    assert f'/opt/venv/bin/slackbridge items set {ALERTS}:{TS} --status "<estado' in a
    assert f'/opt/venv/bin/slackbridge items set {QA}:{TS2} --status "in-progress|done' in q


def test_the_bin_defaults_to_the_one_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACKBRIDGE_BIN", raising=False)
    assert items.update_command("C1:1.000001", "question").startswith("slackbridge items set ")


def test_a_reply_prompt_names_the_item_and_its_handler(store: alerts.AlertStore) -> None:
    """Rule: a reply reaches the subagent handling the item, by the id the ledger holds."""
    config = cfg()
    deliver(top(QA, TS, "duda"), store)
    bare = alerts.classify(reply(QA, TS2, TS, "x"), config, store)
    assert bare is not None
    assert f"Item: {QA}:{TS}" in alerts.build_prompt(bare, config)
    assert "SendMessage" not in alerts.build_prompt(bare, config)
    items.ItemLedger().set_status(f"{QA}:{TS}", None, handler="agent-7f3a")
    handled = alerts.classify(reply(QA, "1795564601.0", TS, "x"), config, store)
    assert handled is not None and handled.handler == "agent-7f3a"
    assert (
        "Reenvía esta respuesta al subagente agent-7f3a con SendMessage "
        "(si ya no existe, lanza uno nuevo con el contexto del ítem)"
    ) in alerts.build_prompt(handled, config)


# --- a failed hand-over -------------------------------------------------------------
def test_a_failed_dispatch_is_marked_in_the_ledger(
    store: alerts.AlertStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude, "agents", lambda: [])

    def boom(*a: object, **k: object) -> None:
        raise OSError("no claude")

    monkeypatch.setattr(subprocess, "run", boom)
    event = alerts.classify(top(QA, TS, "duda"), cfg(), store)
    assert event is not None
    assert alerts.dispatch(event, cfg(), store, LOG) == "error"
    entry = items.ItemLedger().get(f"{QA}:{TS}")
    assert entry is not None and entry["status"] == items.DISPATCH_ERROR


# --- keys -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "key"),
    [
        (f"{ALERTS}:{TS}", f"{ALERTS}:{TS}"),
        (f"https://ws.example.test/archives/{ALERTS}/p1795564509630819", f"{ALERTS}:{TS}"),
        (
            f"https://ws.example.test/archives/{QA}/p1795564600000100?thread_ts={TS}&cid={QA}",
            f"{QA}:{TS}",
        ),
        (f"  <https://ws.example.test/archives/{ALERTS}/p1795564509630819>  ", f"{ALERTS}:{TS}"),
    ],
)
def test_keys_and_permalinks_parse(raw: str, key: str) -> None:
    assert items.parse_key(raw) == key


@pytest.mark.parametrize("raw", ["", "C1", "C1:abc", "https://example.test/x/p123"])
def test_a_malformed_key_is_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        items.parse_key(raw)


# --- status vocabulary -----------------------------------------------------------
@pytest.mark.parametrize("status", ["in-progress", "done", "skipped"])
def test_a_question_takes_its_vocabulary(status: str) -> None:
    assert items.validate_status("question", status) == status


@pytest.mark.parametrize("status", ["Closed - Done", "dispatched", "taken-by-other"])
def test_a_question_refuses_anything_else(status: str) -> None:
    with pytest.raises(ValidationError):
        items.validate_status("question", status)


def test_an_alert_takes_any_non_empty_status() -> None:
    assert items.validate_status("alert", " QA - In Review ") == "QA - In Review"
    assert items.validate_status("alert", "taken-by-other") == "taken-by-other"
    with pytest.raises(ValidationError):
        items.validate_status("alert", "   ")


# --- the file --------------------------------------------------------------------
def test_the_path_honours_its_variables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACKBRIDGE_ITEMS_FILE")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert items.items_path() == tmp_path / "xdg" / "slackbridge" / "items.json"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert items.items_path() == Path.home() / ".local" / "state" / "slackbridge" / "items.json"


def test_a_corrupt_ledger_is_refused_and_left_alone(_isolated_items_ledger: Path) -> None:
    """Rule: unreadable is not empty — empty would mean "re-send everything"."""
    _isolated_items_ledger.parent.mkdir(parents=True)
    _isolated_items_ledger.write_text("{not json")
    ledger = items.ItemLedger()
    with pytest.raises(ConfigError):
        ledger.admit(channel=QA, ts=TS, kind="question", ticket=None, permalink="")
    assert _isolated_items_ledger.read_text() == "{not json"


def test_set_on_an_unknown_key_creates_nothing() -> None:
    with pytest.raises(NotFoundError):
        items.ItemLedger().set_status(f"{QA}:{TS}", "done")
    assert items.ItemLedger().all() == []


def _admit_many(path: str, channel: str, count: int) -> None:
    ledger = items.ItemLedger(Path(path))
    for i in range(count):
        ledger.admit(
            channel=channel, ts=f"1795564509.{i:06d}", kind="question", ticket=None, permalink=""
        )


def test_two_concurrent_writers_lose_no_entry(_isolated_items_ledger: Path) -> None:
    """Rule: the listener and the CLI write together; read-modify-write holds the lock."""
    ctx = multiprocessing.get_context("fork")
    procs = [
        ctx.Process(target=_admit_many, args=(str(_isolated_items_ledger), ch, 150))
        for ch in ("C0WRITERA", "C0WRITERB")
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    assert len(items.ItemLedger().all()) == 300


# --- the CLI -----------------------------------------------------------------------
def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    with pytest.raises(SystemExit) as exit_:
        run(cli.app, list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    return int(exit_.value.code or 0), json.loads(out[-1]) if out else {}


@pytest.fixture
def seeded(store: alerts.AlertStore) -> None:
    deliver(top(ALERTS, TS, ALERT_TEXT), store)
    deliver(top(QA, TS2, "duda"), store)


@pytest.mark.usefixtures("seeded")
def test_cli_lists_and_filters(capsys: pytest.CaptureFixture[str]) -> None:
    code, env = invoke(capsys, "items", "list", "--json")
    assert (
        code == 0
        and env["ok"]
        and [r["key"] for r in env["data"]]
        == [
            f"{ALERTS}:{TS}",
            f"{QA}:{TS2}",
        ]
    )
    _, env = invoke(capsys, "items", "list", "--kind", "question", "--json")
    assert [r["key"] for r in env["data"]] == [f"{QA}:{TS2}"]
    _, env = invoke(capsys, "items", "list", "--status", "done", "--json")
    assert env["data"] == []


@pytest.mark.usefixtures("seeded")
def test_cli_gets_by_key_or_permalink(capsys: pytest.CaptureFixture[str]) -> None:
    link = f"https://ws.example.test/archives/{ALERTS}/p1795564509630819"
    code, env = invoke(capsys, "items", "get", link, "--json")
    assert code == 0 and env["data"]["key"] == f"{ALERTS}:{TS}"
    assert env["data"]["ticket"] == "ABC-123"


@pytest.mark.usefixtures("seeded")
def test_cli_set_appends_to_the_history(capsys: pytest.CaptureFixture[str]) -> None:
    key = f"{ALERTS}:{TS}"
    code, env = invoke(
        capsys,
        "items",
        "set",
        key,
        "--status",
        "QA - In Review",
        "--note",
        "n",
        "--by",
        "triage",
        "--json",
    )
    assert code == 0 and env["data"]["status"] == "QA - In Review"
    invoke(capsys, "items", "set", key, "--handler", "agent-1", "--json")
    entry = items.ItemLedger().get(key)
    assert entry is not None
    assert [h["status"] for h in entry["history"]] == [
        "dispatched",
        "QA - In Review",
        "QA - In Review",
    ]
    assert entry["history"][1]["by"] == "triage" and entry["history"][2]["handler"] == "agent-1"
    assert (entry["note"], entry["handler"]) == ("n", "agent-1")


@pytest.mark.usefixtures("seeded")
def test_cli_set_refuses_unknown_keys_and_bad_statuses(capsys: pytest.CaptureFixture[str]) -> None:
    code, env = invoke(capsys, "items", "set", f"{QA}:1.000001", "--status", "done", "--json")
    assert code != 0 and env["error"] == "NotFoundError"
    code, env = invoke(capsys, "items", "set", f"{QA}:{TS2}", "--status", "QA - Done", "--json")
    assert code != 0 and env["error"] == "ValidationError"
    code, env = invoke(capsys, "items", "set", f"{QA}:{TS2}", "--json")
    assert code != 0 and env["error"] == "ValidationError"
    entry = items.ItemLedger().get(f"{QA}:{TS2}")
    assert entry is not None and entry["status"] == "dispatched"
