# cloudprobe

Read-only forensics for a GKE + Cloudflare deployment. It answers *what is
happening out there* and *did the thing I just shipped make anything worse* —
and it cannot change either.

```bash
uvx --from apps/cloudprobe cloudprobe --help
```

| Command | |
|---|---|
| `cloudprobe gke probe-failures` | readiness/liveness failures in a window, by pod |
| `cloudprobe gke metrics` | node and pod metrics from Cloud Monitoring |
| `cloudprobe trace-task` | every log line mentioning an id, across the three field names it can hide under |
| `cloudprobe cloudflare requests` | edge requests for a host and path |
| `cloudprobe cloudflare sql` | read-only SQL against the Log Explorer |
| `cloudprobe k8s-diagram` / `k8s-chart` | the topology, from your Helm chart |
| `cloudprobe watch` | is the deploy making anything worse: merges, error baselines, volume, platform-vs-control |

Every command takes a global `--json` (before the subcommand) and answers with
one envelope. **Nothing writes.** There is no `--force` and no `--apply`.

## Configuration

| | |
|---|---|
| `CLOUDPROBE_CLUSTER` | the GKE cluster to read |
| `CLOUDPROBE_PROJECT` | the GCP project |
| `CLOUDPROBE_HOST` | the host whose Cloudflare zone to query |
| `CLOUDPROBE_CHART` | the Helm chart directory the topology is read from |
| `CF_API_TOKEN` / `CF_ZONE_ID` | Cloudflare credentials, resolved through the keyring too |

**None of them has a default, and that is the design.** A wrong cluster does not
produce an error, it produces *an answer* — an incident window built from the
wrong environment looks exactly like evidence. So an unconfigured cluster is
refused:

```
$ cloudprobe trace-task abc123
error no cluster: set CLOUDPROBE_CLUSTER or pass --cluster
an empty cluster would emit cluster_name="", which matches nothing and reads
exactly like 'there were no such logs'
```

`metrics` keeps an explicit escape hatch: `cluster=""` queries across every
cluster **on purpose**. Three states rather than two, because collapsing
"unconfigured" into "everything" is how a deliberate opt-out becomes an accident.

## The two techniques worth stealing

**Alert below the floor, not below the average.** Volume swings by hour and by
weekday, so "down 30% on yesterday" fires every Monday. The floor is the
*minimum* that hour of day reached over the last 14 days — crossing it is
something that has not happened once in a fortnight.

**Compare a platform against a control.** A backend problem moves every client;
a release problem moves one. A raw error rate cannot tell those apart, so `watch`
alerts when the subject degrades *and the control does not follow it*.

## No database driver

`watch --volume-table` needs a count from a database, and this tool holds no
credential and no driver. `--volume-command` receives the SQL on stdin and prints
JSON rows, so it works against MySQL, Postgres or a CSV — and the credential
stays with you.

## Development

```bash
cd apps/cloudprobe
uv sync && uv run pytest        # 72 tests
uv run ruff check src tests && uv run mypy src
```

## Provenance

Extracted from a private repository, where the defaults were one company's
cluster, zone, project and an absolute path into one person's home directory.
Removing them was most of the work; refusing rather than guessing was the rest.
