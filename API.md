# API Reference

HTTP endpoints exposed by the controller and worker pods.

> The controller serves a **live, generated OpenAPI spec** at
> `GET /openapi.json` and an interactive (dark-mode) Swagger UI at
> `GET /docs` — both always match the running code. This document is the
> human-readable companion with worked examples. (The checked-in
> `openapi.yaml` is hand-maintained and may lag; prefer `/openapi.json`.)

The controller is reachable via the NodePort Service in
`manifests/controller.yaml` (default `30081`). Workers are not normally
exposed externally — their endpoints are documented here for
diagnostics, Prometheus scraping, and direct `kubectl port-forward`.

Examples use `192.168.2.2:30081` for the controller (replace with your
MicroK8s host IP) and `localhost:8080` for the worker (after a
`kubectl port-forward`). Placeholders like `<app>` refer to whatever
app name you're working with.

> **Apps vs. roles.** A template describes a topology of **apps**. Each app
> materialises into one Kubernetes "role" — the `role=` pod label and the
> `wt-<name>-<app>` resource names — so the **observability** responses
> (`/overview`, `/measurements/*`, `/graph`) still group their results under a
> `roles` key, keyed by app name.

## One controller, one template

A controller manages **exactly one template** — the topology for its
site/VM. The template routes are therefore **singular and take no name**
(`/template`, not `/templates/<name>`). The template's `name` field still
exists internally because every Kubernetes resource is named
`wt-<name>-<app>`. POSTing a second, differently-named template while one
is materialised returns `409`; re-POSTing the same name re-materialises
idempotently.

Every JSON response carries a top-level **`timestamp`** (controller-local
ISO 8601, with offset) recording when it was generated — except `/graph`,
whose shape is dictated by the Grafana Node Graph panel.

The controller serves three groups of endpoints:

- **Template** (`/template`) — create, inspect, patch, tear down the topology.
- **Observability** (`/overview`, `/measurements/*`, `/graph`) — query
  Kubernetes state, live worker gauges, and Prometheus-backed windows.
- **Worker** (port 8080) — per-pod status and metrics (not on the controller).

---

# Template

## `POST /template`

Materialise the topology. Validates the template (shape, cycle detection
on the app graph, `placements`/`node_site_mapping`/`network_links`
cross-references), computes the resolved `x` for each app via topological
propagation, and creates one Deployment + ConfigMap + Service per app. After
the pods come up it resolves peer IPs and writes them back (Phase 2). If the
template declares `network_links`, the controller also applies `tc netem`/`htb`
shaping on each site's node NIC (`controller/netem.py`) — this is the one part
of materialisation that isn't pure Kubernetes API calls; see TEMPLATE.md.

Idempotent for the **same** `name`: re-POSTing updates the existing
resources via PATCH (use it as a "reapply" if you lose track of state). For
surgical changes prefer `PATCH /template`.

**Body schema:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | yes | k8s name (lowercase + digits + `-`). Becomes the resource prefix `wt-<name>-<app>` |
| `x` | number **or** `{app: number}` map | yes | The signal that drives the topology. A number seeds every source app; a map seeds each named source app independently (downstream apps always derive their x). See "x propagation" |
| `apps` | object | yes | Map of `app_name → app_spec`. See below |
| `edges` | array | optional | App-to-app links: list of `{"from": app, "to": app}` |
| `node_site_mapping` | array | optional | Site name → k8s node registry: `[{"k8s_node", "site_name"}, ...]`. Used by `placements` and `network_links` |
| `network_links` | array | optional | Declared latency/bandwidth between sites: `[{"from", "to", "rtt_ms", "bandwidth_mbps"?}, ...]`, applied via `tc netem` |
| `runtime_scenarios` | array | optional | Time-varying load schedule for the scenario runner. See "Scenario runner" below |

This is a **brownfield** API: you already own a cluster with nodes; a template
describes the **workload** and **where its pods run on your nodes**. Full field
reference (including `placements`/`node_site_mapping`/`network_links`) is in
[TEMPLATE.md](TEMPLATE.md).

**App spec:**

```json
{
  "cpu": {"a": 0, "b": 100},
  "ram": {"a": 0, "b": 128},
  "net": {"a": 0.2, "b": 0},
  "placements": [ { "site": "site-A", "count": 3 } ]
}
```

Targets are computed per pod as `value = a * x_app + b` (see "x propagation").
`count` is required unless `placements` is used (then it's derived from the
sum of placement entry counts). Where the pods actually run is decided by
**exactly one** of `placement` (object) or `placements` (array) — both are
**enforced**, not advisory:

- **`placement`** (legacy, raw node labels/hostnames) — one of:
  - **`{ "on": { <label>: <value>, ... }, "spread"?: true }`** — schedule the
    app's `count` pods on nodes matching these labels (`nodeSelector`); `spread`
    distributes them evenly across the matches (`topologySpreadConstraints`).
    Use the labels your nodes already carry (`kubectl get nodes --show-labels`).
  - **`{ "node": "<hostname>" }`** — pin all the app's pods to that one node.
  - **`{ "nodes": [ { "node": "<hostname>", "count": N }, ... ] }`** — run N pods
    on each named node. The controller materialises **one Deployment per node**
    (all sharing the app's Service). The counts must **sum to `count`**.
- **`placements`** (array, site names resolved through the template's
  `node_site_mapping` — no node labelling needed) — a list of:
  - **`{ "site": "<site_name>", "count": N }`** — N pods pinned to that site's
    node.
  - **`{ "sites": ["<site_name>", ...], "count": N }`** — N pods at **each**
    listed site (one Deployment per site).

  Every site named must appear in the template's `node_site_mapping` — a `400`
  otherwise, not a silently `Pending` pod.

Omit both to fall back to the controller's **`DEFAULT_NODE`** env
(`manifests/controller.yaml`), or schedule anywhere if that's unset —
`DEFAULT_NODE` also overrides `placements`' site resolution entirely, handy
for single-node testing without editing the template.

Validation (`400`): `placement` and `placements` are mutually exclusive; within
`placement`, only one mode at a time, `spread` only with `on`, `on` a
non-empty label map, `node` a non-empty hostname, `nodes` counts positive ints
summing to `count` with no node repeated; within `placements`, every
`site`/`sites` name must resolve via `node_site_mapping`. Pods referencing a
label/host no node has stay `Pending` (standard Kubernetes) — this can only
happen with `placement`, since `placements` is validated against
`node_site_mapping` at POST time.

**Scenario runner (`runtime_scenarios`):**

```json
"runtime_scenarios": [
  { "phase_id": "warmup", "start_min": 0,  "end_min": 5,  "x": 10 },
  { "phase_id": "peak",   "start_min": 5,  "end_min": 15, "x": { "ingest-a": 80, "ingest-b": 20 } },
  { "phase_id": "cooldown","start_min": 15, "end_min": 25, "x": 20 }
]
```

A template may carry a `runtime_scenarios` list to drive a **time-varying
load** instead of a fixed `x`. Each phase is a wall-clock window (minutes
since the template was POSTed) holding the input `x` at a value; `x` takes the
**same two forms as the top-level `x`** — a single number for every source app,
or an `{app: number}` map for per-source starting values — and downstream apps
re-derive their own `x` from it (the same x-propagation model as a static
template). The controller runs one scenario runner at a time: it steps `x` to
the active phase on a 5s clock by re-resolving and re-patching every app's
ConfigMap, and workers hot-reload each change within ~2s — no pod restart. A
phase's `x` **replaces** the previous phase's wholesale (a map phase fully
defines that window's starting points; sources it omits run at 0 for that
window). After the last phase it holds that phase's `x`. Editing the schedule
with a PATCH restarts the clock from now; a DELETE (or a PATCH that removes
`runtime_scenarios`) stops it. The runner is in-memory, so a controller restart
resumes it from the start of the schedule. Phases need numeric `start_min` /
`end_min` and a valid `x` with `end_min > start_min`; a bad schedule (including
an `x` map that names a non-source app) is rejected with a 400.

**Example:**

```bash
curl -X POST http://192.168.2.2:30081/template \
  -H 'Content-Type: application/json' \
  -d @templates/iot-pipeline.json
```

**Response (201):** a materialisation summary — plus the controller-**derived**
values you can't read off the template: each app's **`resolved_x`** (its
propagated cascade value), the **`deployments`** it materialises into
(`replicas` + `node_selector` each, so a `nodes`/`placements` split and the
`DEFAULT_NODE` fallback are both visible), and its resolved **`peers`**.
`node_site_mapping` echoes back as the resolved `{site_name: k8s_node}` map (or
`null` if the template declared none); `network_links` as a count (or `null`)
— same lightweight-indicator pattern as `runtime_scenarios`, since the full
declared content round-trips via `GET /template`.

```json
{
  "timestamp": "2026-06-12T14:00:00+01:00",
  "name": "<name>",
  "x": 10,                              // as posted: a number or {app: number} map
  "default_node": "microk8s-vm",        // the controller's DEFAULT_NODE (or null)
  "runtime_scenarios": 4,               // number of phases (or null if none)
  "node_site_mapping": { "site-A": "edge-1", "site-B": "edge-2" },  // resolved map, or null
  "network_links": 2,                   // number of declared links, or null
  "apps": {
    "ingest": {
      "count": 3,
      "placement": null,
      "placements": [ { "site": "site-A", "count": 2 }, { "site": "site-B", "count": 1 } ],
      "deployments": [
        { "replicas": 2, "node_selector": { "kubernetes.io/hostname": "edge-1" } },
        { "replicas": 1, "node_selector": { "kubernetes.io/hostname": "edge-2" } }
      ],
      "resolved_x": 10.0                // the x this app's formulas evaluate at
    },
    "store": { "...": "..." }
  },
  "peers": {
    "ingest": ["wt-<name>-store"],
    "store": []
  },
  "warnings": [                         // anything only partly done; [] if none
    "wt-<name>-store: 1 of 2 pods Ready after 30s; peers were wired to the Ready ones only — check for Pending or crashing pods",
    "network link site-A ↔ site-B is NOT shaped: 'multipass' not found where the controller runs, …"
  ],
  "network_shaping": {                  // per-link tc outcome (see GET /overview)
    "status": "not_applied",            // none | applied | partial | not_applied
    "links": { "site-A ↔ site-B": { "applied": false, "reason": "…" } }
  }
}
```

A 201 means the template was accepted and applied — not that every part took
effect. Check `warnings`: pods that never became Ready (unschedulable,
crashing) and `network_links` that couldn't be shaped are listed there rather
than failing the request.

**Status codes:**

| Code | Meaning |
|------|---------|
| 201 | Materialised |
| 400 | Invalid JSON, validation failure, cycle in the app graph, bad `placement`/`placements`, a `network_links`/`placements` site name with no matching `node_site_mapping` entry, or a node (`node_site_mapping[].k8s_node`, `placement.node`/`nodes`, or the controller's `DEFAULT_NODE`) that isn't in the cluster — nothing is created |
| 409 | A *different* template is already materialised — PATCH or DELETE it first |
| 502 | A k8s API call failed mid-materialisation. Partial resources may exist — re-POST or DELETE to clean up |

## `GET /template`

The materialised template, in full (the original POST body, reconstructed
from ConfigMap annotations — there's no in-memory state on the controller),
with a `timestamp` prepended.

```bash
curl http://192.168.2.2:30081/template | python3 -m json.tool
```

`404` if nothing is materialised. (A re-POST of the GET response round-trips
cleanly — the controller strips the injected `timestamp` on the way in.)

## `PATCH /template`

Apply a partial update. Deep-merges the patch body into the existing
template, re-runs validation (including cycle detection), and
re-materialises. Workers pick up the new ConfigMap within ~60s (kubelet
sync) — **no pod restart** unless the change adds/removes pods. A patch that
changes `network_links` re-applies `tc netem`/`htb` shaping immediately
(`materialise()` calls `netem.apply()` unconditionally on every
materialisation, including PATCH-triggered ones).

The merged result is validated against the same schema as a `POST` body
(400 on anything `POST` would reject), and the response carries the same
`warnings` and `network_shaping` fields as `POST`.

**Merge semantics:**

- Dicts merge recursively. `{"apps": {"<app>": {"net": {"a": 0.3}}}}`
  changes only that app's `net.a`.
- Scalars and lists in the patch **replace** the existing value. To change
  one edge, send the whole new `edges` list. `network_links` and
  `node_site_mapping` are lists too, so to retune a link send the whole
  `network_links` list.
- **Dot-path shorthand**: a flat key with dots expands to nested JSON, e.g.
  `{"apps.web.cpu.a": 5}` ≡ `{"apps": {"web": {"cpu": {"a": 5}}}}`. (Dot-path
  reaches into objects only, not list elements — patch `network_links` whole.)
- The `name` and any injected `timestamp` in the body are ignored.

**Common patches:**

```bash
# Change x for every source — the whole topology recascades
curl -X PATCH http://192.168.2.2:30081/template -d '{"x": 20}'

# Bump one source app's starting x (map merges: other sources unchanged)
curl -X PATCH http://192.168.2.2:30081/template -d '{"x": {"ingest-b": 90}}'

# Bump one app's network — downstream x values re-resolve
curl -X PATCH http://192.168.2.2:30081/template -d '{"apps.web.net.a": 0.30}'

# Scale an app (adds/removes pods; re-resolves x + re-wires peers)
curl -X PATCH http://192.168.2.2:30081/template -d '{"apps.api.count": 4}'

# Re-place an app (send the whole placement object — replaces the old one)
curl -X PATCH http://192.168.2.2:30081/template \
  -d '{"apps": {"web": {"placement": {"on": {"tier": "fog"}}}}}'

# Retune a declared link's latency/bandwidth (send the whole network_links list)
curl -X PATCH http://192.168.2.2:30081/template \
  -d '{"network_links": [{"from": "site-A", "to": "site-B", "rtt_ms": 50}]}'
```

**Response (200):** `{"timestamp", "name", "template": {…merged…}, "peers": {…}}`

**Status codes:** `200` patched · `400` validation/cycle/bad placement ·
`404` no template materialised · `502` k8s failure.

## `DELETE /template`

Tear down every resource for the materialised template: Deployments,
Services, and ConfigMaps. If the template declared `network_links`, this also
clears the `tc netem`/`htb` shaping rules from every site's node NIC
(`controller/netem.py`) — the one piece of cleanup that isn't a Kubernetes
API delete.

```bash
curl -X DELETE http://192.168.2.2:30081/template
# → {"timestamp":"…","name":"<name>","deleted":<count>}
```

`404` if nothing is materialised. `deleted` counts the Kubernetes objects
removed.

---

# Observability

Read endpoints fuse three data sources behind one envelope:

- **Kubernetes live state** — Deployments (desired replicas) and Pods
  (phase, readiness, restarts, node, age) via the k8s API.
- **Instantaneous worker gauges** — scraped straight from each pod's
  `/metrics` (no Prometheus dependency).
- **Windowed aggregates** — via the Prometheus HTTP API (`PROM_URL`), for
  the `/measurements/range` and `/measurements/periods` endpoints.

### Site identity & federation

Every observability response carries a `site` block:

```json
"site": {"id": "<SITE_ID>", "tier": "<SITE_TIER>"}
```

One controller runs per VM; `SITE_ID` / `SITE_TIER` are set per controller
via env (`manifests/controller.yaml`; default `"local"` / `"unknown"`). The
block lets a future federation gateway fan out to many controllers and merge
responses by site with no schema change.

> **Two unrelated "site" concepts, same word.** This `site` block identifies
> *which controller/VM* answered — for a future multi-controller federation
> gateway. It's unrelated to a template's `node_site_mapping` "site" names
> (see TEMPLATE.md), which identify *nodes within one cluster* for pod
> placement and `network_links` shaping. Don't confuse the two.

### Timezone

Timestamps in responses (and naive request timestamps to
`/measurements/*`) use the controller's local timezone, set via the `TZ`
env var — `manifests/controller.yaml` ships it set to `Europe/London`; the
code itself falls back to `UTC` if `TZ` is unset or unrecognised (logged as a
warning). Internals compute in absolute unix time, so this is presentation
only; rendered values carry their UTC offset so they stay unambiguous.

## `GET /overview`

Site-wide snapshot — every materialised template with per-app desired vs
ready replica counts (under a `roles` key, keyed by app name) and a
pod-health rollup. **Subsumes the old `/health`
endpoint**: the `"ok": true` field is present whenever the web layer is up
(the k8s readiness probe itself is now a cheap TCP-socket check, not an HTTP
hit on this heavier endpoint).

```bash
curl http://192.168.2.2:30081/overview | python3 -m json.tool
```

**Response (200):**
```json
{
  "timestamp": "2026-06-12T14:00:00+01:00",
  "ok": true,
  "site": {"id": "local", "tier": "cloud"},
  "namespace": "cloud-native-emulator",
  "templates": [
    {"name": "<name>", "source": "http",
     "roles": {"<app>": {"desired": 2, "ready": 2}},
     "pods": {"total": 3, "ready": 3}}
  ],
  "prometheus": {"available": true, "url": "http://…:9090"},
  "network_shaping": {"status": "applied",
                      "links": {"site-A ↔ site-B": {"applied": true}}}
}
```

`network_shaping` is the outcome of the most recent materialise (`status`
`none` when the template declares no links, `unknown` until the first
materialise after a controller restart).

## `GET /measurements/now`

Rich, instantaneous per-app status (under a `roles` key, keyed by app name).
For each app: desired/ready replicas, the resolved `x`, app-level target and
actual sums (CPU/RAM/NET), and a per-pod list fusing k8s state with that pod's
current worker gauges. Also returns measured app→app edge traffic. Live
scrapes only — no Prometheus.

Pods that exist in k8s but aren't in Endpoints yet (starting up, not Ready)
are surfaced with an empty `metrics: {}`, so an in-progress scale-up is
visible rather than silently missing.

```bash
curl http://192.168.2.2:30081/measurements/now | python3 -m json.tool
```

**Response (200, abridged):**
```json
{
  "timestamp": "2026-06-12T14:00:00+01:00",
  "site": {"id": "local", "tier": "cloud"},
  "name": "<name>",
  "source": "http",
  "roles": {
    "<app>": {
      "desired": 2, "ready": 2, "x": 10.0,
      "targets": {"cpu_millicores": 100.0, "ram_mb": 128.0, "net_mbps": 4.0},
      "actuals": {"cpu_millicores": 98.2, "ram_mb": 130.1, "net_mbps": 3.9},
      "pods": [{"name": "wt-<name>-<app>-abc123", "ip": "10.1.0.42",
                "node": "node-1", "phase": "Running", "ready": true,
                "restarts": 0, "age_seconds": 312,
                "metrics": {"x": 10.0, "target_cpu_millicores": 50.0, "...": "..."}}]
    }
  },
  "edges": [{"from": "<app-a>", "to": "<app-b>", "mbps": 3.912}],
  "prometheus": {"available": null}
}
```

`404` if nothing is materialised.

## `GET /measurements/range`

CPU/RAM/network for the template, **aggregated between two points in
time** — the "just tell me the numbers" endpoint. Returns scalars: the
template-wide target and actual summed across pods then reduced over the
interval, **always** broken down by app (under a `roles` key) and accompanied
by an `x` block.

**Query params:**

| Param | Default | Notes |
|-------|---------|-------|
| `start` | `end` − 15m | ISO 8601 (`2026-06-10T11:00:00`) or unix epoch seconds. No offset ⇒ controller-local time; `Z`/offset honoured |
| `end` | now | Same formats as `start` |
| `resources` | `cpu,ram,net` | Comma-separated subset. Unknown values → 400 |

`totals` carries `target_avg`, `actual_avg`, `actual_min`, `actual_max`;
the per-app breakdown carries the two averages. The `x` block is split by
provenance: **`input`** (source apps, whose x *is* the template's x) and
**`derived`** (downstream apps, x propagated from upstream egress). Both
are averaged — not summed — across each app's pods.

```bash
# Last 15 minutes (defaults)
curl -s "http://192.168.2.2:30081/measurements/range" | python3 -m json.tool

# A fixed hour, CPU + net only
curl -s "http://192.168.2.2:30081/measurements/range?start=2026-06-10T11:00:00&end=2026-06-10T12:00:00&resources=cpu,net" \
  | python3 -m json.tool
```

**Response (200, abridged):**
```json
{
  "timestamp": "2026-06-12T14:00:00+01:00",
  "site": {"id": "local", "tier": "cloud"},
  "name": "<name>",
  "start": "2026-06-10T11:00:00+01:00",
  "end":   "2026-06-10T12:00:00+01:00",
  "window": "3600s",
  "totals": {"cpu_millicores": {"target_avg": 980.0, "actual_avg": 951.2,
                                "actual_min": 902.0, "actual_max": 1010.5}, "...": "..."},
  "roles":  {"<app>": {"cpu_millicores": {"target_avg": 484.8, "actual_avg": 470.1}, "...": "..."}},
  "x": {"input":   {"<source-app>": 40.0},
        "derived": {"<downstream-app>": 31.5}},
  "prometheus": {"available": true, "url": "http://…:9090"}
}
```

**Errors:** `400` on an unknown resource, `start >= end`, or a window over
31 days. `404` if nothing is materialised. If Prometheus is unreachable,
returns 200 with `prometheus.available: false` and empty
`totals`/`roles`/`x` — use `/measurements/now` for a live, no-Prometheus
alternative.

## `GET /measurements/periods`

The **last `count` chunks of `chunk` each, ending now**, aggregated
separately — for seeing how the numbers move over a run rather than one
flattened average.

**Query params:** `resources` as above, plus:

| Param | Required | Notes |
|-------|----------|-------|
| `count` | yes | How many chunks to return, counting back from now |
| `chunk` | yes | Length of each chunk, e.g. `90s`, `10m`, `1h` (min `30s`, the scrape interval) |

You say how many chunks and how long each one is; the range is exactly
`count × chunk`, ending now. e.g. `count=4&chunk=11m` → the last 44 minutes
as 4 eleven-minute periods.

```bash
# The last 44 minutes as 4 eleven-minute periods
curl -s "http://192.168.2.2:30081/measurements/periods?count=4&chunk=11m" \
  | python3 -m json.tool
```

**Response (200, abridged):**
```json
{
  "timestamp": "…", "site": {"…": "…"}, "name": "<name>",
  "start": "…", "end": "…", "window": "2640s",
  "chunk": "660s", "count": 4,
  "periods": [
    {"start": "…", "end": "…",
     "totals": {"…": "…"}, "roles": {"…": "…"},
     "x": {"input": {"…": "…"}, "derived": {"…": "…"}}}
  ],
  "prometheus": {"available": true, "url": "http://…:9090"}
}
```

**Errors:** `400` for a missing/malformed `chunk` or `count`, or more than
100 chunks. `404` if nothing is materialised.

## `GET /graph`

Grafana Node Graph payload (nodes + measured edges) for the materialised
template — the data source behind `grafana/grafana-nodegraph.json`. **No
`timestamp`** — the panel rejects unknown top-level keys.

| Param | Default | Notes |
|-------|---------|-------|
| `view` | `role` | `role` (default): one node per app, stats summed across its pods, edges app→app. `pods`: one node per pod, raw pod→pod edges |

```bash
curl http://192.168.2.2:30081/graph | python3 -m json.tool
curl "http://192.168.2.2:30081/graph?view=pods" | python3 -m json.tool
```

`404` if nothing is materialised.

## `GET /metrics` (controller)

Prometheus text-format scrape endpoint on the **controller** itself — distinct
from each worker pod's own `/metrics` (below). Publishes the materialised
template's *declared* `network_links` values, recomputed from the live
template on every scrape (so a PATCH retune shows up at once):

| Metric | Labels | Meaning |
|--------|--------|---------|
| `emulator_configured_rtt_ms` | `pair` (`"site-A ↔ site-B"`) | Declared round-trip latency per site pair |
| `emulator_configured_bandwidth_mbps` | `pair` | Declared per-direction bandwidth cap per site pair (only pairs that declared one) |
| `emulator_link_shaping_applied` | `pair` | `1` if that link's `tc` shaping is in effect as of the last materialise, `0` if it was skipped or failed — where it's `0`, the configured values are intent only |

Compare these against the workers' *measured* `worker_peer_rtt_ms` /
`worker_peer_egress_mbps` to see how the real `tc netem`-shaped path compares
to what the template declares. Cleared (no series) when nothing is
materialised or the template has no `network_links`. Scraped by the
`emulator-controller` PodMonitor in `manifests/monitoring.yaml`.

```bash
curl -s http://192.168.2.2:30081/metrics | grep -E "^emulator_"
```

---

## x propagation — the model behind the template

The template's `x` is a *signal* that flows through the app graph, not a
global constant shared by every app.

```
x (scalar or {app: n})  ──▶  each source app (no inbound edges)
                  │           starts at its own seed value
                  │ NET formula:  per_pod_egress = max(0, net.a * x + net.b)
                  │ app total:    count * per_pod_egress
                  ▼
                  downstream app's x = Σ (upstream app totals)
                  │ same formula again with this app's net coefficients
                  ▼
                  …
```

For each app, the controller computes `x_app` via a topological pass
(Kahn's algorithm). A source app's x is its seed — the single `x` number, or
its own entry in the `{app: number}` map (unlisted sources default to 0); a
downstream app's x is the sum of upstream app-total egress. All three of CPU,
RAM, and NET formulas evaluate at this resolved x. The `/measurements/range`
and `/periods` responses split these into `x.input` (sources — where per-source
starting values appear) and `x.derived` (downstream). This is what lets one
template run several independent sub-systems (DAGs) from different starting x.

Self-edges (intra-app mesh, `from == to`) are legal traffic-wise but
ignored for x-resolution. Cycles in the app graph are rejected at validate
time (`400`, `role graph has a cycle involving: …`). The resolved x per app
is logged at materialise time and visible per pod as the `worker_input_x`
gauge.

---

# Worker

Workers expose three endpoints on port `8080`. They're not exposed via
NodePort by default. Reach them with:

```bash
microk8s kubectl port-forward <pod-name> 8080:8080
```

## `GET /health`

```bash
curl http://localhost:8080/health
# → {"ok":true}
```

## `GET /status`

The worker's full `STATE` dict — what it's currently doing.

```json
{
  "running": true,
  "x": <resolved-x-for-this-app>,
  "cpu_millicores": <cpu.a * x + cpu.b>,
  "ram_mb": <ram.a * x + ram.b>,
  "net_mbps": <net.a * x + net.b>,
  "formulas": {"cpu": {"a": …, "b": …}, "ram": {"a": …, "b": …}, "net": {"a": …, "b": …}},
  "peers": ["<peer-ip-1>", "<peer-ip-2>", …]
}
```

For a templated worker, `x` is the *resolved* x for this pod's app, and
`peers` is the list of concrete pod IPs (not Service names).

## `GET /metrics`

Prometheus text-format scrape endpoint. Gauges exposed:

| Metric | Meaning |
|--------|---------|
| `worker_input_x` | Resolved x for this app |
| `worker_target_cpu_millicores` / `_ram_mb` / `_net_mbps` | `a * x + b` per resource |
| `worker_actual_cpu_millicores` | 15s rolling average from cgroup `cpu.stat` |
| `worker_actual_ram_mb` | cgroup working set (matches cAdvisor / Grafana) |
| `worker_actual_net_mbps` | 15s rolling egress rate from `psutil.net_io_counters` |
| `worker_cpu_stress_millicores` | CPU the feedback loop currently asks stress-ng to generate |
| `worker_peer_egress_mbps{peer}` | Measured egress to a specific peer (from iperf3 interval reports). `peer` is the peer pod's **IP address** — not a resolved app/role name |
| `worker_peer_rtt_ms{peer}` | TCP-handshake RTT to a peer pod, probed every 15s — the real cluster network path (the controller injects nothing; compare against the declared `emulator_configured_rtt_ms`). `peer` is the peer pod's IP address |

All gauges are scraped via the PodMonitor in `manifests/monitoring.yaml`, which
copies each pod's `template`/`role` labels onto every scraped series
(`podTargetLabels`) — so `role` is a real label you can group by, but there is
**no `peer_name` label**: only the raw `peer` IP. Grouping by a `peer_name`
label in PromQL (as some Grafana panels do) silently collapses every peer
into one series, since a non-existent label groups as an empty string — group
by `peer` (the IP) instead if you need to distinguish destinations.

```bash
curl -s http://localhost:8080/metrics | grep -E "^worker_"
```

---

# Quick reference

## Template

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/template` | Materialise the topology (409 if a different one exists) |
| GET | `/template` | The materialised template, in full |
| PATCH | `/template` | Partial update — merges, re-resolves, re-materialises |
| DELETE | `/template` | Tear the topology down |

## Observability

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/overview` | Site-wide snapshot (subsumes `/health`) |
| GET | `/measurements/now` | Live per-app state + gauges + edges |
| GET | `/measurements/range` | CPU/RAM/net aggregated over `?start`–`?end` |
| GET | `/measurements/periods` | The last `?count` chunks of `?chunk` each, ending now |
| GET | `/graph` | Grafana Node Graph payload (`?view=role\|pods`) |
| GET | `/metrics` | Controller's own Prometheus scrape — declared `network_links` metrics |
| GET | `/docs`, `/openapi.json` | Swagger UI (dark) and the generated spec |

## Worker (port-forward to access)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Worker liveness |
| GET | `/status` | Worker state (resolved x, formulas, peers) |
| GET | `/metrics` | Prometheus scrape |
