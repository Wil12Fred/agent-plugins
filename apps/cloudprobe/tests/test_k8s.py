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
