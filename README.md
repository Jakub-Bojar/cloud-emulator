# Cloud-Native Emulator

A resource-emulation framework for the EEECS summer research project
*"High-Fidelity Emulation Framework for Cloud-Native Applications."*

You describe a **topology** of apps (e.g. gateway → auth/api → cache → db)
as JSON; the controller materialises one Kubernetes Deployment + ConfigMap +
Service per app and runs synthetic CPU, RAM, and network load in every pod
sized to per-app formulas. An input signal `x` propagates through the
app graph, so changing it recasts load across the whole topology (and a single
template can seed several independent sub-systems at different `x`).
The template can also declare `node_site_mapping` (a site name → k8s node
registry), per-app `placements` resolved through it, and `network_links`
(round-trip latency + bandwidth) between sites — enforced pod placement and
real `tc netem`/`htb` link shaping, no node labelling required.

Each controller is the **site API for one VM** and manages exactly one
template. Every observability response is tagged with a `{site}` block
(edge / fog / cloud), so the same system can be run on multiple VMs and
merged by a federation layer later.

## How it works

- **Template** — a named set of `apps` connected by directional `edges`.
  Each app has linear formulas for CPU (millicores), RAM (MB), and network
  (Mbps): `value = a·x_app + b`, and `count` replicas (required unless
  `placements` is used — then derived from the placement entries). (Each app
  materialises as one Kubernetes "role" — the `role=` label and
  `wt-<name>-<app>` resource names — so the observability endpoints still
  report results under a `roles` key, keyed by app name.)
- **x propagation** — source apps use the template's `x` (a single number for
  all sources, or an `{app: number}` map giving each source its own starting
  value); a downstream app's `x` is the sum of upstream app-total egress,
  resolved via a topological pass over the graph (cycles are rejected). The map
  form lets one template run several independent sub-systems from different
  starting points.
- **Workers** generate real load: `stress-ng` (CPU), an anonymous `mmap`
  (RAM, with a feedback nudger), and `iperf3` (per-peer network traffic).
- **Two-phase materialisation** — pods are created first, then their real
  IPs are resolved and written back so iperf3 clients target concrete peers.
- **Placement, enforced** — an app's `placement` (raw node labels/hostnames)
  or `placements` (site names resolved through a template-declared
  `node_site_mapping`) pins its pods to real nodes via `nodeSelector` — no
  `kubectl label node` step needed for the `placements`/`node_site_mapping`
  form. See TEMPLATE.md.
- **`network_links` (latency + bandwidth shaping)** — declares round-trip
  latency (`rtt_ms`) and an optional bandwidth cap (`bandwidth_mbps`) between
  site pairs, e.g. `{"from": "site-A", "to": "site-B", "rtt_ms": 20,
  "bandwidth_mbps": 500}`. The controller applies these with `tc netem`/`htb`
  on each site's node NIC (`controller/netem.py`) and republishes the
  declared values as the `emulator_configured_rtt_ms` /
  `emulator_configured_bandwidth_mbps` metrics for comparison against the
  workers' measured figures.
- **Observability** — workers export Prometheus gauges (target vs actual per
  resource, plus per-peer RTT); the controller fuses k8s state + live scrapes
  + Prometheus into the measurement endpoints, and serves a Grafana
  node-graph of measured edges.

```
          POST /template {x, apps, edges, node_site_mapping, network_links}
   operator ─────────────────────────────────▶ ┌──────────────┐
   GET /overview · /measurements/* · /graph     │ controller   │ NodePort 30081
                                                │ materialiser │
                                                └──────┬───────┘
                              create/patch via k8s API │
                 ┌───────────────────────┬─────────────┴─────────────┐
                 ▼                        ▼                           ▼
         Deployment+CM+Svc        Deployment+CM+Svc            Deployment+CM+Svc
           (app: gateway)           (app: api ×N)                (app: db)
              worker pods  ◀── iperf3 peer traffic ──▶  worker pods
                 │ /metrics scrape    (network_links shaped via tc netem)
                 ▼
          Prometheus ──▶ Grafana
```

## Documentation

| Doc | What's in it |
|-----|--------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System diagrams, the x-propagation model, two-phase materialisation, site placement & network shaping, federation roadmap |
| [TEMPLATE.md](TEMPLATE.md) | The POST-body schema field-by-field: what's required, what each field does, what's enforced vs. purely declarative |
| [API.md](API.md) | Full HTTP API reference with worked examples |
| `GET /docs`, `GET /openapi.json` | Live Swagger UI (dark) + generated OpenAPI spec — always matches the running code |
| [RUNBOOK.md](RUNBOOK.md) | Build, deploy, drive, observe, debug, tear down |

## Prerequisites

- Docker + a Docker Hub account (examples use `jp36/…` — swap in your own)
- MicroK8s on a reachable host, with `metrics-server` enabled
- Optional: a Prometheus Operator install (kube-prometheus-stack) for the
  PodMonitor and the windowed measurement endpoints
- No node labels required for the `placements`/`node_site_mapping` placement
  form — the template names real k8s node hostnames directly and the
  controller pins pods with `nodeSelector`. If you use `network_links`,
  shaping needs `multipass` reachable from wherever the controller process
  runs (see RUNBOOK.md's "Known limitations" — the shipped containerized
  deployment doesn't have this by default).

## Quick start

Replace `jp36` with your Docker Hub user and `192.168.2.2` with your
MicroK8s host IP.

```bash
# 1. Build & push. The controller MUST build from the repo root (its
#    Dockerfile copies manifests/worker-template.yaml into the image).
docker build -t jp36/emulator-worker:latest worker/
docker push jp36/emulator-worker:latest
docker build -f controller/Dockerfile -t jp36/emulator-controller:latest .
docker push jp36/emulator-controller:latest

# 2. Deploy the controller (+ monitoring if you use Prometheus Operator).
kubectl apply -f manifests/controller.yaml
kubectl apply -f manifests/monitoring.yaml

# 3. Materialise a topology (one controller manages one template).
curl -X POST http://192.168.2.2:30081/template \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "fb",
    "x": 10,
    "apps": {
      "frontend": {"count": 1, "cpu":{"a":10,"b":100}, "ram":{"a":2,"b":32}, "net":{"a":0.2,"b":2}},
      "backend":  {"count": 2, "cpu":{"a":5,"b":50},  "ram":{"a":1,"b":16}, "net":{"a":0.1,"b":1}}
    },
    "edges": [{"from":"frontend","to":"backend"}]
  }'

# 4. Observe.
curl -s http://192.168.2.2:30081/overview | python3 -m json.tool
curl -s http://192.168.2.2:30081/measurements/now | python3 -m json.tool
curl -s "http://192.168.2.2:30081/measurements/range?resources=cpu,ram,net" | python3 -m json.tool

# 5. Scale an app (re-resolves x + re-wires peers) via PATCH.
curl -X PATCH http://192.168.2.2:30081/template \
  -H 'Content-Type: application/json' -d '{"apps.backend.count": 4}'

# 6. Tear down.
curl -X DELETE http://192.168.2.2:30081/template
```

## Project layout

```
cloud-native-emulator/
├── README.md
├── ARCHITECTURE.md · API.md · RUNBOOK.md · TEMPLATE.md
│       (GET /openapi.json is the generated spec — always matches the running code)
├── controller/
│   ├── Dockerfile
│   ├── app.py            FastAPI HTTP routes: /template CRUD + /graph + measurements
│   ├── api.py            observability endpoints (overview / measurements)
│   ├── linkspec.py       network_links shape validation + configured metrics
│   ├── netem.py          tc netem/htb link shaping via multipass exec
│   ├── runner.py         scenario runner — steps x through runtime_scenarios
│   ├── watcher.py        ConfigMap reconciler for declarative (non-HTTP) templates
│   ├── prom.py           Prometheus HTTP API client
│   ├── graph.py          topology scrape + edge measurement
│   ├── materialiser.py   template → k8s resources, two-phase create + IP resolve
│   └── k8s.py            shared in-cluster k8s API client
├── worker/
│   ├── Dockerfile
│   ├── worker.py         entrypoint + configure() funnel
│   ├── loads.py          stress-ng / mmap / iperf3 load generators
│   ├── metrics.py        cgroup sampler, Prometheus gauges, RAM nudger
│   ├── state.py          shared state + tunable env constants
│   └── watcher.py        filesystem watchdog on the mounted ConfigMap
├── manifests/
│   ├── controller.yaml          ServiceAccount + RBAC + Pod + NodePort Service
│   ├── worker-template.yaml      per-app blueprint, baked into the controller image
│   ├── monitoring.yaml           PodMonitor for the templated worker pods + controller
│   └── monitoring-values.yaml    kube-prometheus-stack Helm values overlay
├── templates/                    ready-made topologies (iot-pipeline, smart-campus,
│                                 cdn-live-video — the latter shows placements +
│                                 node_site_mapping + network_links)
└── grafana/                      dashboards incl. grafana-network.json (per-link latency + bandwidth)
```
