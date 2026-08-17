"""The architecture diagram is derived, not drawn."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from opscore.errors import NotFoundError

from cloudprobe import k8s

VALUES = {
    "deployments": [
        "auth-service",
        "lessons-service",
        "queue-reservation",
    ],
    "ingress": {
        "hosts": [
            {
                "host": "api.example.com",
                "paths": [
                    {
                        "path": "/auth/*",
                        "backend": {"service": {"name": "auth-service"}},
                    },
                    {
                        "path": "/lessons/*",
                        "backend": {"service": {"name": "lessons-service"}},
                    },
                ],
            }
        ]
    },
}


@pytest.fixture
def chart(tmp_path: Path) -> Path:
    (tmp_path / "values.yaml").write_text(yaml.safe_dump(VALUES), encoding="utf-8")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "app-secrets.yaml").write_text("kind: Secret\n", encoding="utf-8")
    return tmp_path


def test_routes_come_from_the_chart(chart: Path) -> None:
    parsed = k8s.load(chart)
    assert parsed.hosts["api.example.com"] == [
        ("/auth/*", "auth-service"),
        ("/lessons/*", "lessons-service"),
    ]


def test_only_secret_names_are_read_never_values(chart: Path) -> None:
    parsed = k8s.load(chart)
    assert parsed.secrets == ["app-secrets"]
    rendered = k8s.render(parsed, chart_dir=chart, values_name="values.yaml")
    assert "kind: Secret" not in rendered


def test_a_service_with_no_ingress_is_called_out(chart: Path) -> None:
    # queue-reservation is deployed but has no public route: it is reachable
    # only from inside the cluster. That distinction is the useful part.
    rendered = k8s.render(k8s.load(chart), chart_dir=chart, values_name="values.yaml")
    assert "No public ingress" in rendered
    assert "queue-reservation" in rendered


def test_mermaid_node_ids_drop_the_characters_mermaid_rejects() -> None:
    assert k8s._node_id("api.example.com") == "api_example_com"
    assert k8s._node_id("auth-service") == "auth_service"


def test_the_output_is_marked_generated(chart: Path) -> None:
    rendered = k8s.render(k8s.load(chart), chart_dir=chart, values_name="values.yaml")
    assert k8s.MARKER in rendered
    assert "do not hand-edit" in rendered


def test_a_missing_values_file_is_a_not_found(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        k8s.load(tmp_path)


def test_the_edge_hop_is_part_of_the_picture(chart: Path) -> None:
    # Cloudflare sits in front of the LB; a diagram starting at the LB hides
    # where domain rules and edge blocking actually happen.
    rendered = k8s.render(k8s.load(chart), chart_dir=chart, values_name="values.yaml")
    assert "Cloudflare" in rendered
    assert "CF --> LB" in rendered


# --- the generated header is a citation, so it carries a revision -----------


def test_the_header_stamps_the_commit_the_chart_was_read_at(tmp_path) -> None:
    """Rule: a generated document that names a source path must say which
    revision that path was true at.

    A path and a line number are only true against one revision; without the
    commit a reader cannot distinguish "this moved" from "this was always
    wrong". The generator is the only party that knows for certain, because it
    is holding the file.
    """
    import subprocess

    from cloudprobe import k8s

    chart = tmp_path / "some-chart"
    chart.mkdir()
    (chart / "values.yaml").write_text("x: 1\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "chart"],
    ):
        subprocess.run(command, cwd=chart, check=True, capture_output=True)

    header = k8s.render(k8s.Chart(), chart_dir=chart, values_name="values.yaml")

    stamp = k8s.chart_commit(chart)
    assert stamp is not None
    assert f"@{stamp}" in header


def test_outside_a_repository_the_header_says_unknown_rather_than_guessing(tmp_path) -> None:
    """The control, and the more important half.

    An unpacked chart has no revision. Inventing one, or silently omitting the
    stamp so the line looks the same as a stamped one, would be worse than the
    problem: the reader would trust a citation nobody can check.
    """
    from cloudprobe import k8s

    chart = tmp_path / "loose-chart"
    chart.mkdir()

    header = k8s.render(k8s.Chart(), chart_dir=chart, values_name="values.yaml")

    assert k8s.chart_commit(chart) is None
    assert "revision unknown" in header
