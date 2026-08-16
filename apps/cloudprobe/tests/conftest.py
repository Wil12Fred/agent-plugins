"""A configured cluster, so every test states its own scope.

`cloudprobe` refuses to build a filter without a cluster: an empty one emits
`cluster_name=""`, which matches nothing and reads exactly like "there were no
such logs". The refusal itself is tested in `test_logs.py`; everywhere else a
cluster is configured so the test is about the thing it names.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _configured_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDPROBE_CLUSTER", "prod-cluster")
    for module in ("cloudprobe.logs", "cloudprobe.gke", "cloudprobe.metrics"):
        monkeypatch.setattr(f"{module}.DEFAULT_CLUSTER", "prod-cluster", raising=False)
