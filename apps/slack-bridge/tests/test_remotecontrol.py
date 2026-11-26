"""Remote Control status of named sessions, read from Claude's per-session state files."""

from __future__ import annotations

import json
from pathlib import Path

from slackbridge import remotecontrol


def _write(base: Path, pid: int, **fields: object) -> None:
    (base / f"{pid}.json").write_text(json.dumps({"pid": pid, **fields}))


def test_a_session_with_a_bridge_id_is_connected_and_gets_its_url(tmp_path: Path) -> None:
    """Rule: `bridgeSessionId` present = Remote Control connected; URL claude.ai/code/<id>."""
    _write(tmp_path, 10, name="support", bridgeSessionId="session_X", startedAt=1)
    _write(tmp_path, 11, name="support-worker", startedAt=1)
    live = remotecontrol.live_sessions(tmp_path, alive=lambda pid: True)
    rows = {r["name"]: r for r in remotecontrol.status(["support", "support-worker"], live)}
    assert rows["support"]["remote_control"] is True
    assert rows["support"]["url"] == "https://claude.ai/code/session_X"
    assert rows["support-worker"]["remote_control"] is False and rows["support-worker"]["found"]


def test_a_stale_file_whose_process_is_gone_does_not_count(tmp_path: Path) -> None:
    """Rule: a state file left by a dead process must not report a session as connected."""
    _write(tmp_path, 20, name="support", bridgeSessionId="session_old", startedAt=1)
    live = remotecontrol.live_sessions(tmp_path, alive=lambda pid: pid != 20)
    (row,) = remotecontrol.status(["support"], live)
    assert row == {"name": "support", "found": False, "remote_control": False}


def test_the_newest_live_session_with_the_name_is_the_one_reported(tmp_path: Path) -> None:
    """Rule: after a relaunch two files may share a name; the newest one is the truth."""
    _write(tmp_path, 30, name="w", bridgeSessionId="session_old", startedAt=1)
    _write(tmp_path, 31, name="w", startedAt=2)
    live = remotecontrol.live_sessions(tmp_path, alive=lambda pid: True)
    (row,) = remotecontrol.status(["w"], live)
    assert row["pid"] == 31 and row["remote_control"] is False


def test_unreadable_state_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "40.json").write_text("{not json")
    _write(tmp_path, 41, name="ok", bridgeSessionId="session_Y", startedAt=1)
    live = remotecontrol.live_sessions(tmp_path, alive=lambda pid: True)
    assert [s["pid"] for s in live] == [41]


def test_the_cli_exits_1_when_a_session_is_not_connected(tmp_path: Path) -> None:
    """Rule: the command gates scripts, so "not connected" must be a non-zero exit — the
    first version printed ok=false and still exited 0."""
    import os
    import subprocess
    import sys

    (tmp_path / "sessions").mkdir()
    env = {**os.environ, "CLAUDE_HOME": str(tmp_path), "OPSCORE_ENV_FILE": str(tmp_path / "x")}
    bin_ = Path(sys.executable).parent / "slackbridge"
    done = subprocess.run(
        [str(bin_), "remote-control", "check", "--session", "nobody", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert '"ok": false' in done.stdout
