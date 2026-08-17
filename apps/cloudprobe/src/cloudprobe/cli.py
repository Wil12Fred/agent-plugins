"""``cloudprobe`` — read-only cluster and edge forensics."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from opscore.env import project_root
from opscore.errors import ConfigError, ValidationError
from opscore.guard import Consequence, WriteIntent, check_write
from opscore.output import get_output

from cloudprobe import cloudflare, gke, k8s, logs, metrics, watch

app = typer.Typer(
    name="infra",
    help="Infrastructure forensics: GKE probe failures, Cloudflare logs, architecture diagram.",
    no_args_is_help=True,
)

gke_app = typer.Typer(name="gke", help="GKE incident forensics.", no_args_is_help=True)
app.add_typer(gke_app, name="gke")


@app.callback()
def _root(
    json_mode: Annotated[
        bool, typer.Option("--json", help="Emit exactly one JSON envelope on stdout.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Suppress progress on stderr.")] = False,
) -> None:
    """Options every command shares. `--json` goes before the subcommand."""
    from opscore.output import Output, set_output

    set_output(Output(json_mode=json_mode, quiet=quiet))


@gke_app.command("probe-failures")
def probe_failures(
    service: Annotated[
        list[str], typer.Option("--service", help="Service name to inspect (repeatable).")
    ],
    baseline_start: Annotated[str, typer.Option("--baseline-start", help="RFC3339.")],
    baseline_end: Annotated[str, typer.Option("--baseline-end", help="RFC3339.")],
    incident_start: Annotated[str, typer.Option("--incident-start", help="RFC3339.")],
    incident_end: Annotated[str, typer.Option("--incident-end", help="RFC3339.")],
    project: Annotated[str, typer.Option("--project", help="GCP project id.")] = (
        gke.DEFAULT_PROJECT
    ),
    cluster: Annotated[
        str,
        typer.Option("--cluster", help="GKE cluster. dev and prod share pod-name prefixes."),
    ] = gke.DEFAULT_CLUSTER,
    namespace: Annotated[str, typer.Option("--namespace", help="Kubernetes namespace.")] = (
        gke.DEFAULT_NAMESPACE
    ),
    limit: Annotated[int, typer.Option("--limit", help="Max log entries per window.")] = (
        gke.DEFAULT_LIMIT
    ),
) -> None:
    """Compare probe-failure rates between a baseline and an incident window.

    Timeouts (`context deadline exceeded`) and refusals (`connection refused`)
    are counted separately because they mean opposite things: the first is a
    pod answering too slowly (CPU starvation), the second is nothing listening.
    Rates, not counts, are compared — the windows are different lengths.
    """
    rows = gke.compare(
        list(service),
        gke.Window(baseline_start, baseline_end),
        gke.Window(incident_start, incident_end),
        project=project,
        cluster=cluster,
        namespace=namespace,
        limit=limit,
    )
    get_output().table(
        rows,
        columns=[
            "service",
            "failure",
            "baseline_count",
            "incident_count",
            "baseline_rate_per_min",
            "incident_rate_per_min",
            "pct_change",
        ],
        title="probe failures: baseline vs incident",
    )


cf_app = typer.Typer(
    name="cloudflare", help="Cloudflare Log Explorer forensics.", no_args_is_help=True
)
app.add_typer(cf_app, name="cloudflare")


@cf_app.command("requests")
def cf_requests(
    host: Annotated[
        str, typer.Option("--host", help="clientRequestHost to filter on.")
    ] = cloudflare.DEFAULT_HOST,
    path: Annotated[
        str, typer.Option("--path", help="clientRequestPath to filter on.")
    ] = cloudflare.DEFAULT_WEBHOOK_PATH,
    days: Annotated[int, typer.Option("--days", help="How far back to look.")] = 30,
    limit: Annotated[int, typer.Option("--limit", help="Maximum rows.")] = 500,
    summary: Annotated[
        bool, typer.Option("--summary", help="Aggregate instead of listing rows.")
    ] = False,
) -> None:
    """Query what the edge saw for one host and path.

    Cloudflare sits in front of the load balancer, so a request rejected at the
    edge never reaches your logs. When a partner says "we called your webhook
    and got nothing", this is the only place that can say whether the call
    arrived at all.
    """
    sql = cloudflare.build_query(host=host, path=path, days=days, limit=limit)
    rows = cloudflare.run(cloudflare.resolve_zone_id(host), sql)
    out = get_output()
    if summary:
        out.result(cloudflare.summarise(rows))
        return
    out.result({"query": sql, "rows": rows})


@cf_app.command("sql")
def cf_sql(
    query: Annotated[str, typer.Argument(help="A SELECT against http_requests.")],
    host: Annotated[
        str, typer.Option("--host", help="Host whose zone to query.")
    ] = cloudflare.DEFAULT_HOST,
    zone_id: Annotated[
        str | None, typer.Option("--zone-id", help="Zone id, if you already know it.")
    ] = None,
) -> None:
    """Run an arbitrary read-only Log Explorer query.

    Guarded by the same read-only check the database uses: anything that is not
    a SELECT is refused before it leaves this machine.
    """
    get_output().result(cloudflare.run(zone_id or cloudflare.resolve_zone_id(host), query))


@app.command("k8s-diagram")
def k8s_diagram(
    chart: Annotated[Path | None, typer.Option("--chart", help="Helm chart directory.")] = None,
    values: Annotated[
        str, typer.Option("--values", help="Values file inside the chart.")
    ] = k8s.DEFAULT_VALUES,
    out: Annotated[Path | None, typer.Option("--out", help="Where to write the diagram.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Render without writing.")] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm-prod-write", help="Actually write the file.")
    ] = False,
) -> None:
    """Regenerate the architecture diagram from the Helm chart.

    Derived from the same values.yaml that decides what is deployed, so the
    diagram cannot drift from reality — regenerating it is the only way it
    changes. Only secret NAMES are read.
    """
    # Resolve once and hand the SAME path to both. Passing the raw option to
    # `render` meant a chart supplied through the environment was read
    # correctly and then reported as `<chart>`.
    resolved = k8s.resolve_chart(chart)
    parsed = k8s.load(resolved, values)
    document = k8s.render(parsed, chart_dir=resolved, values_name=values)

    target = out or (project_root() / "docs/diagrams/k8s-architecture.md")
    if not check_write(
        WriteIntent(
            consequence=Consequence.REPOSITORY,
            action="overwrite the architecture diagram",
            target=str(target),
        ),
        dry_run=dry_run,
        confirmed=confirm,
    ):
        get_output().result({"target": str(target), "written": False, "diagram": document})
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")

    get_output().result(
        {
            "written": str(target),
            "hosts": len(parsed.hosts),
            "routes": sum(len(r) for r in parsed.hosts.values()),
            "deployments": len(parsed.deployments),
            "secret_templates": len(parsed.secrets),
        }
    )


@app.command("k8s-chart")
def k8s_chart(
    chart: Annotated[Path | None, typer.Option("--chart", help="Helm chart directory.")] = None,
    values: Annotated[
        str, typer.Option("--values", help="Values file inside the chart.")
    ] = k8s.DEFAULT_VALUES,
) -> None:
    """Report what the chart declares: ingress routes, deployments, secret names."""
    get_output().result(k8s.load(chart, values).as_dict())


@gke_app.command("metrics")
def gke_metrics(
    project: Annotated[str, typer.Option("--project", help="GCP project id.")],
    start: Annotated[str, typer.Option("--start", help="Window start, RFC3339.")],
    end: Annotated[str, typer.Option("--end", help="Window end, RFC3339.")],
    metric: Annotated[
        str, typer.Option("--metric", help="Monitoring metric type.")
    ] = metrics.NODE_CPU,
    cluster: Annotated[
        str,
        typer.Option(
            "--cluster",
            help="GKE cluster. Empty queries every cluster in the project, deliberately.",
        ),
    ] = gke.DEFAULT_CLUSTER,
    resource_filter: Annotated[
        str, typer.Option("--resource-filter", help='Extra filter, e.g. resource.labels.x="y".')
    ] = "",
    hot: Annotated[
        float, typer.Option("--hot", help="Threshold above which a series counts as hot.")
    ] = metrics.DEFAULT_HOT_THRESHOLD,
) -> None:
    """Print a metric per minute across an incident window.

    Use it to separate the two failure modes that look identical from inside a
    pod: a container throttled by its own limit pins high, while one starved at
    the node level *falls* — it is not being scheduled. Compare
    `kubernetes.io/node/cpu/allocatable_utilization` against
    `kubernetes.io/container/cpu/limit_utilization` for the same window.

    `max` is the column that matters: an average across 30 nodes hides the one
    that saturated.
    """
    series = metrics.fetch(
        project, metric, start=start, end=end, cluster=cluster, resource_filter=resource_filter
    )
    samples = metrics.summarise(series, hot_threshold=hot)
    get_output().table(
        [s.as_dict() for s in samples],
        columns=["timestamp", "max", "avg", "series", "hot"],
        title=f"{metric} — {len(samples)} minute(s), {len(series)} series",
    )


@app.command("trace-task")
def trace_task(
    task_id: Annotated[str, typer.Argument(help="Queue task / message id to follow.")],
    cluster: Annotated[
        str, typer.Option("--cluster", help="GKE cluster; dev and prod share the image.")
    ] = logs.DEFAULT_CLUSTER,
    container: Annotated[
        str | None, typer.Option("--container", help="Narrow to one container.")
    ] = None,
    freshness: Annotated[
        int, typer.Option("--freshness", help="How many minutes back to look.")
    ] = logs.DEFAULT_FRESHNESS_MINUTES,
) -> None:
    """Follow one queue task through the logs, oldest first.

    Searches all three field names a task id appears under — `data.taskId` when
    the producer enqueues, `data.messageId` once the consumer parsed it, and
    inside `data.rawTask` for the consumed event, which is logged *before*
    parsing and so is the only one that survives a malformed payload. Querying
    one of the three finds a third of the story.

    `cluster_name` is always applied: dev and prod run the same container image,
    so a query without it returns production entries for a dev investigation.
    """
    entries = logs.read(task_id, cluster=cluster, container=container, freshness_minutes=freshness)
    get_output().table(
        logs.timeline(entries),
        columns=["timestamp", "event", "container", "cluster"],
        title=f"{task_id}: {len(entries)} log line(s)",
    )


@app.command("watch")
def watch_deploy(
    mr: Annotated[
        list[str] | None,
        typer.Option("--mr", help="Merge request as project!iid. Repeatable."),
    ] = None,
    baseline: Annotated[
        list[str] | None,
        typer.Option("--baseline", help="Error code as NAME=count. Repeatable."),
    ] = None,
    container: Annotated[
        str | None,
        typer.Option("--container", help="k8s container whose logs carry the error codes."),
    ] = None,
    volume_table: Annotated[
        str | None,
        typer.Option("--volume-table", help="Table to compare against its 14-day hourly floor."),
    ] = None,
    volume_column: Annotated[
        str, typer.Option("--volume-column", help="Timestamp column on that table.")
    ] = "insDate",
    volume_command: Annotated[
        str | None,
        typer.Option(
            "--volume-command",
            help="Shell command that reads SQL on stdin and prints JSON rows.",
        ),
    ] = None,
    subject_ua: Annotated[
        str | None,
        typer.Option("--subject-ua", help="SQL LIKE pattern for the platform under watch."),
    ] = None,
    control_ua: Annotated[
        str | None,
        typer.Option("--control-ua", help="SQL LIKE pattern for the control platform."),
    ] = None,
    alert_pct: Annotated[
        float, typer.Option("--alert-pct", help="Edge error rate that counts as degraded.")
    ] = 12.0,
    ref: Annotated[
        str, typer.Option("--ref", help="Default branch — merging into it is the deploy.")
    ] = "master",
    freshness: Annotated[
        int, typer.Option("--freshness-minutes", help="Log window for the error codes.")
    ] = 20,
) -> None:
    """Take one reading of a deploy's health: merges, error codes, volume, platform.

    A single pass, always — no loop, no sleep. Run it from cron, from `/loop`,
    or by hand; a command that blocks for hours cannot be composed, cannot be
    tested, and cannot report a meaningful exit code.

    Every probe is optional and independent, so the useful invocation is the
    small one:

        cloudprobe watch --mr myorg/auth-service!327 \\
            --baseline GLOBAL.ERROR_USER_DELETED=0 \\
            --container lessons-service \\
            --volume-table membership_lesson

    Two of the probes encode a judgement worth keeping. **Volume alerts only
    below that hour's 14-day minimum** — blunt on purpose, because a percentage
    drop fires every Monday. **The platform probe needs a control**: a backend
    problem moves every client, a release problem moves one, and without
    `--control-ua` the two are indistinguishable.

    Read-only throughout. Exits 7 when any probe alerts, so a scheduler can act
    on the status alone.
    """
    out = get_output()
    readings: list[watch.Reading] = []
    merges: list[dict[str, object]] = []

    for raw in mr or []:
        request = watch.MergeRequest.parse(raw)
        state = watch.merge_state(request)
        if state is None:
            merges.append({"mr": str(request), "state": "unknown", "reason": "glab did not answer"})
            continue
        row: dict[str, object] = {"mr": str(request), **state.as_dict()}
        if state.state == "merged":
            row["deploy"] = watch.deploy_state(request.project, ref)
        merges.append(row)

    for name, expected in watch.parse_baseline(baseline or []).items():
        entries = logs.read(name, container=container, freshness_minutes=freshness, limit=200)
        readings.append(watch.over_baseline(name, len(entries), expected))

    if volume_table and volume_command:
        # No database driver here, and that is the design. The floor rule is
        # arithmetic over two numbers; which database produced them is the
        # caller's business. `--volume-command` receives the SQL on stdin and
        # prints JSON rows, so this works against MySQL, Postgres or a CSV —
        # and cloudprobe never has to hold a credential.
        rows = watch.rows_from_command(
            volume_command, watch.floor_query(volume_table, volume_column)
        )
        if rows:
            first = rows[0]
            readings.append(
                watch.below_floor(
                    int(first["last_hour"] or 0),
                    int(first["floor_value"] or 0),
                    label=f"{volume_table} per hour",
                )
            )

    if subject_ua:
        start, end = watch.closed_window()
        host = cloudflare.DEFAULT_HOST
        if not host:
            raise ConfigError(
                "no host: set CLOUDPROBE_HOST or pass --subject-ua with a configured zone",
                detail="the Cloudflare zone is resolved from the host it serves",
            )
        zone = cloudflare.resolve_zone_id(host)
        subject = watch.edge_rate(cloudflare.run(zone, watch.edge_rate_sql(subject_ua, start, end)))
        control = (
            watch.edge_rate(cloudflare.run(zone, watch.edge_rate_sql(control_ua, start, end)))
            if control_ua
            else None
        )
        readings.append(watch.against_control(subject, control, alert_pct=alert_pct))

    alerting = [r for r in readings if r.alert]
    out.table(
        [r.as_dict() for r in readings],
        columns=["probe", "value", "threshold", "alert", "detail"],
        title=f"watch: {len(readings)} probe(s), {len(alerting)} alerting",
        ok=not alerting,
        message=f"{len(alerting)} probe(s) crossed" if alerting else None,
    )
    out.result({"merges": merges, "probes": [r.as_dict() for r in readings]})
    if alerting:
        raise ValidationError(
            f"{len(alerting)} probe(s) alerting: " + ", ".join(r.name for r in alerting)
        )


def main() -> None:
    """Console-script entry point.

    The shared runner loads the `.env`, renders our own errors as one envelope,
    and answers a *usage* error under ``--json`` instead of letting Click write
    to stderr and leave stdout empty. See :mod:`opscore.cli`.
    """
    from opscore.cli import run

    run(app)
