# Template Reference

Everything about the JSON you `POST /template`: every field, whether it's
required, and what it does. This system is **brownfield** — you already own a
Kubernetes cluster with nodes. A template describes your **workload** and
**where its pods run on your nodes**; the controller schedules it onto your
cluster. It does *not* model or provision infrastructure — the cluster is the
source of truth for what nodes actually exist.

For the HTTP endpoints see [API.md](API.md); for build/deploy/ops see
[RUNBOOK.md](RUNBOOK.md).

## Mental model

- **`apps` + `edges`** — the workload. Each app becomes Kubernetes
  Deployment(s) of worker pods generating real CPU/RAM/network load; `edges`
  are directional app→app links driving real iperf3 traffic and the `x` cascade.
- **`placement`** (per app, object form) — where its pods run using your
  existing node labels/hostnames directly. Enforced via `nodeSelector` /
  topology spread / per-node Deployments.
- **`placements` + `node_site_mapping`** (per app, array form) — where its
  pods run using logical **site names**, resolved through the template's own
  `node_site_mapping` registry to real node hostnames. No node labelling
  needed at all — the template is fully self-describing.
- **`network_links`** — declared latency/bandwidth between sites (by the same
  site names `node_site_mapping` defines). The controller applies these with
  `tc netem`/`htb` on each site's node NIC.
- **`x`** — a single load signal that propagates through the app graph.

> **One controller = one template.** Re-POSTing the same `name` re-materialises
> idempotently; a different `name` while one exists returns `409`.

## Top-level fields

| Field | Type | Required | Effect |
|-------|------|----------|--------|
| `name` | string | **yes** | k8s resource prefix `wt-<name>-<app>` (lowercase + digits + `-`) |
| `apps` | object | **yes** | the workloads that run |
| `x` | number **or** `{app: number}` map | no (default `0`) | the load signal |
| `edges` | array | no (`[]`) | app→app traffic + `x` propagation |
| `node_site_mapping` | array | no | site name → k8s node registry, used by `placements` and `network_links` |
| `network_links` | array | no | declared latency/bandwidth between sites, applied via `tc netem` |
| `runtime_scenarios` | array | no | time-varying `x` schedule |

Unknown fields are accepted and preserved but ignored.

The minimal template is just `name` + `apps`.

---

## `apps` — required

A map of `app_name → app_spec`. Each app becomes one (or more) Kubernetes
Deployments + one Service + one ConfigMap, all labelled `role=<app>`. App names
become resource names, so they must be DNS-label-safe (lowercase alnum + `-`).

### App spec

```json
{
  "count": 3,
  "cpu": {"a": 1.0, "b": 20},
  "ram": {"a": 0.1, "b": 64},
  "net": {"a": 0.2, "b": 1},
  "placement": { "on": { "tier": "edge" }, "spread": true }
}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `count` | integer ≥ 1 | conditional | total pod replicas for this app — required unless `placements` is used (then derived from its entries) |
| `cpu` | `{a, b}` | **yes** | CPU target formula → **millicores** |
| `ram` | `{a, b}` | **yes** | RAM target formula → **MB** |
| `net` | `{a, b}` | **yes** | network egress target → **Mbps** |
| `placement` | object | no | where the pods run, by raw node label/hostname (see below) |
| `placements` | array | no | where the pods run, by site name resolved through `node_site_mapping` (see below) |

**The formula.** Per pod, `target = max(0, a * x_app + b)`, where `x_app` is the
app's resolved x (see [x propagation](#x-propagation)). `a` is the slope per
unit of x; `b` is the constant floor. `cpu` → `stress-ng`, `ram` → `mmap`,
`net` → `iperf3` egress split across peers (an app with no outbound edges
generates none, but can receive).

> Don't guess `a`/`b` for a real workload — measure them. [`calibration/calibrate.py`](calibration/calibrate.py)
> drives a real model on your machine (any Ollama/OpenAI-compatible server;
> text or image, in or out) across a sweep of x and prints a paste-ready
> `cpu`/`ram`/`net` block. See [calibration/README.md](calibration/README.md).

---

## `placement` — where an app's pods run

`placement` is an object using **exactly one** of three modes. It's **enforced**
— the controller schedules pods accordingly. Omit it to use the controller's
`DEFAULT_NODE` fallback, or schedule anywhere if that's unset.

### 1. `on` — match node labels (optionally spread)
Schedule the app's `count` pods on nodes carrying these labels (`nodeSelector`).
Use the labels your nodes already have (`kubectl get nodes --show-labels`). With
`spread: true`, distribute the replicas evenly across matching nodes
(`topologySpreadConstraints`).
```json
"placement": { "on": { "tier": "edge", "region": "eu" }, "spread": true }
```

### 2. `node` — pin to one node
All the app's pods land on this node (by hostname).
```json
"placement": { "node": "edge-1" }
```

### 3. `nodes` — split across nodes with per-node counts
Run specific counts on specific nodes. The controller materialises **one
Deployment per node** (`wt-<name>-<app>-0`, `-1`, …), each pinned by hostname,
all sharing the app's one Service and ConfigMap — so `edges`, peers, and
x-propagation are unaffected. This is how you run **several of the same pod on
different nodes**. The counts must **sum to the app's `count`**.
```json
"placement": { "nodes": [ { "node": "edge-1", "count": 2 }, { "node": "edge-2", "count": 3 } ] }
```

Validation (`400` otherwise): only one mode at a time; `spread` only with `on`;
`on` is a non-empty label map; `node` a non-empty hostname; `nodes` counts are
positive ints summing to `count`, with no node repeated.

> Placement targets your **real cluster**. On a node missing the labels/hostname
> you reference, those pods stay `Pending` (standard Kubernetes) — label your
> nodes or adjust the placement. Use `kubectl get nodes --show-labels` to see
> what's available.

---

## `node_site_mapping` — optional

The template's own site → node registry: maps logical **site names** (used by
`placements` and `network_links`) to real Kubernetes node names. This is the
alternative to node labelling — the controller pins pods directly by hostname,
so `kubectl label node` is never needed.

```json
"node_site_mapping": [
  { "k8s_node": "worker-1", "site_name": "site-A" },
  { "k8s_node": "worker-2", "site_name": "site-B" }
]
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `k8s_node` | string | **yes** | the real k8s node name (`kubectl get nodes`) |
| `site_name` | string | **yes** | the logical name used elsewhere in the template |

`site_name` values must be unique (it's the lookup key); the same `k8s_node`
may back several `site_name`s — e.g. testing multiple sites on one physical
node/VM.

---

## `placements` — where an app's pods run, by site

The array alternative to `placement`. Instead of raw node labels, each entry
names a **site** — resolved through the template's `node_site_mapping` to a
real node, then pinned the same way `placement.node` pins a single node.

```json
"placements": [
  { "site": "site-A", "count": 1 },
  { "sites": ["site-B", "site-C"], "count": 1 }
]
```

| Field | Type | Meaning |
|-------|------|---------|
| `site` | string | exactly `count` pods on this site's node. Use this or `sites`. |
| `sites` | array of strings | `count` pods at **each** listed site (one Deployment per site). Use this or `site`. |
| `count` | integer ≥ 1 | pods at this site (or per site, for `sites`) |

Every site named must appear in the template's `node_site_mapping` — a typo or
missing mapping entry is a `400` at POST time, not a silently `Pending` pod.
The app's top-level `count` is optional here (see the `apps` table above); if
given, it must equal the sum (`site` entries add their count; `sites` entries
add `count × len(sites)`).

`placement` and `placements` are mutually exclusive per app (`400` if both are
set).

---

## `network_links` — optional

Declared round-trip latency and per-direction bandwidth cap between two
sites, applied via `tc netem`/`htb` on each site's node NIC (see
[RUNBOOK.md](RUNBOOK.md) for the underlying mechanism):

```json
"network_links": [
  { "from": "site-A", "to": "site-B", "rtt_ms": 20, "bandwidth_mbps": 500 }
]
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `from` / `to` | string | **yes** | site names — must appear in `node_site_mapping` when one is declared |
| `rtt_ms` | number > 0 | **yes** | round-trip latency; `tc` applies half as one-way delay on each side |
| `bandwidth_mbps` | number > 0 | no | per-direction bandwidth cap |

Links are symmetric — declare each site pair once, in either direction
(`400` on a duplicate or self-link). If the template has no
`node_site_mapping` at all, `network_links` is accepted without a site
cross-check (declarative only, for templates that just want the
`emulator_configured_rtt_ms`/`emulator_configured_bandwidth_mbps` metrics
published without any actual node pinning).

> Actually applying the shaping needs `multipass` reachable from wherever the
> controller process runs — the shipped containerized deployment
> (`manifests/controller.yaml`, a Pod inside the cluster) doesn't have this,
> so shaping silently no-ops there even though `placements` pod placement
> still works. See RUNBOOK.md's "Known limitations".

---

## `edges` — optional

Directional app→app links — the part of the graph that *does things*.
```json
"edges": [ { "from": "frontend", "to": "backend" } ]
```
- `from`/`to` must be names in `apps` (else `400`).
- Every pod of `from` opens iperf3 connections to the Service of `to`
  (1-to-many fan-out when `to.count > 1`).
- Edges drive: traffic, **`x` propagation** (`to`'s x = sum of upstream egress),
  and iperf3 server-pool sizing.
- Self-edges are legal (intra-app mesh) but ignored for x. **Cycles** are a
  `400`.

> **Conserve traffic**: for a `from → to` edge to hit targets,
> `sum(from.net × from.count) ≈ sum(to.net × to.count)`. Not auto-checked.

---

## `x` and `runtime_scenarios`

`x` is the load signal. Two forms:
- **a number** — every source app (no inbound edges) starts there;
- **an `{app: number}` map** — each named **source** app starts at its own value
  (unlisted sources default to 0). Lets one template run several independent
  sub-systems from different starting points. The map may only name source apps.

`runtime_scenarios` is an optional time-varying schedule:
```json
"runtime_scenarios": [
  { "phase_id": "warmup", "start_min": 0, "end_min": 5,  "x": 10 },
  { "phase_id": "peak",   "start_min": 5, "end_min": 15, "x": { "ingest-a": 80, "ingest-b": 20 } }
]
```
Each phase needs numeric `start_min` / `end_min` (`end > start`) and an `x`
(number or per-source map — same rules as the top-level `x`). The runner steps
`x` to the active phase on a 5s clock; workers hot-reload with no pod restart. A
phase's `x` **replaces** the previous one's wholesale; the final phase's value
is held after the schedule ends. The clock is wall-clock from POST — if the host
sleeps mid-run, phases it slept through are skipped (it reconciles to the
correct value on wake).

---

## x propagation

`x` flows through the app graph, it isn't a global constant:
```
x (number or {app: n})  ──▶  each source app starts at its seed value
                  │  per-pod egress = max(0, net.a * x + net.b)
                  │  app total      = count * per-pod egress
                  ▼
                  downstream app's x = Σ (upstream app totals)
                  ▼  …topologically, until every app is resolved
```
All of `cpu`/`ram`/`net` evaluate at the resolved x. Cycles → `400`. The
`/measurements/*` responses split x into `input` (sources) and `derived`.

---

## What the controller derives (not template inputs)

Visible in the `POST /template` response and each app's generated `config.json`,
but computed for you:
- **resolved `x`** per app (the cascade);
- the **Deployments** an app materialises into — `(replicas, node_selector)` per
  Deployment, so a `nodes`/`placements` split and the `DEFAULT_NODE` fallback
  are both visible;
- **`node_site_mapping`** — echoed back as the resolved `{site_name: k8s_node}`
  map (or `null` if the template declared none);
- **`network_links`** — echoed back as a count of declared links (or `null`),
  same lightweight-indicator pattern as `runtime_scenarios`;
- **`peers`** — concrete peer pod IPs;
- **`server_count`** / **`port_offset_by_pod`** — iperf3 wiring.

`DEFAULT_NODE` (a controller env, not a template field) is the fallback node
for any app without a `placement`/`placements` — handy for single-node
testing. It overrides `placements`' site resolution entirely, collapsing
every app onto that one node regardless of `node_site_mapping`.

---

## Lifecycle

- **`POST /template`** → validate, materialise, return a summary: `name`, `x`,
  `default_node`, `node_site_mapping`, `network_links` (count), per-app
  `count`/`placement`/`placements`/resolved `deployments`/`resolved_x`, and
  `peers`.
- **`GET /template`** → the stored template (round-trips cleanly).
- **`PATCH /template`** → deep-merge a partial body and re-materialise. Objects
  merge recursively (`{"apps": {"web": {"cpu": {"a": 5}}}}`); scalars/lists
  replace; dot-paths expand (`{"apps.web.cpu.a": 5}`). To change `placement`,
  send the whole `placement` object.
- **`DELETE /template`** → tear down all Deployments/Services/ConfigMaps.

---

## Full example (brownfield)

```jsonc
{
  "name": "shop",
  "x": 10,
  "apps": {
    "store-front": { "count": 2, "cpu": {"a":1,"b":20}, "ram": {"a":0.1,"b":64}, "net": {"a":0.4,"b":5},
      "placement": { "on": { "tier": "edge" } } },
    "catalog-svc": { "count": 3, "cpu": {"a":2,"b":35}, "ram": {"a":0.3,"b":128}, "net": {"a":0.2,"b":2},
      "placement": { "on": { "tier": "app" }, "spread": true } },
    "postgres":    { "count": 1, "cpu": {"a":1,"b":50}, "ram": {"a":1,"b":512}, "net": {"a":0,"b":0},
      "placement": { "node": "db-1" } },
    "redis":       { "count": 4, "cpu": {"a":0.5,"b":25}, "ram": {"a":0.5,"b":256}, "net": {"a":0,"b":0},
      "placement": { "nodes": [ { "node": "cache-1", "count": 2 }, { "node": "cache-2", "count": 2 } ] } }
  },
  "edges": [
    { "from": "store-front", "to": "catalog-svc" },
    { "from": "catalog-svc", "to": "postgres" },
    { "from": "catalog-svc", "to": "redis" }
  ]
}
```

This example uses raw `placement` (node labels/hostnames you already have).
For the `node_site_mapping` + `placements` + `network_links` form — site
names instead of node labels, plus declared inter-site latency/bandwidth —
see [`templates/cdn-live-video.json`](templates/cdn-live-video.json) and the
multi-node setup walkthrough in [RUNBOOK.md](RUNBOOK.md).

Ready-made templates live in [`templates/`](templates/).
