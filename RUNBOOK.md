# Runbook

Operational reference for the cloud-native emulator: build, deploy, drive,
observe, debug, and tear down. Topologies are ingested over HTTP
(`POST /template`).

Run everything from the repo root. Replace `jp36` with your Docker Hub
username and `192.168.2.2` with your MicroK8s host IP throughout. Pods
live in the `cloud-native-emulator` namespace by default — add
`-n cloud-native-emulator` if your kubectl context targets `default`.

---

## Prerequisites

Installed on your own machine (the Multipass host) before you start:

- [Multipass](https://multipass.run) — creates and runs the VM(s) the cluster lives on
- Docker Desktop (or any Docker daemon) — builds the controller/worker images
- A Docker Hub account, repos public or with `imagePullSecret` configured
- Standalone `kubectl` and `helm` (e.g. `brew install kubectl helm`) — day-to-day
  cluster commands in this runbook run these directly against the cluster's
  kubeconfig, not through `microk8s kubectl`/`microk8s helm3`. "Bootstrap from
  scratch" below covers wiring this up.

If MicroK8s and the monitoring stack aren't running yet, start at "Bootstrap
from scratch" below. If they're already up, skip straight to "Build & push
images".

If the template declares `network_links`, the controller shapes them with
`tc netem`/`htb` via `multipass exec` (see "Multi-node setup" below). **As
currently deployed — the controller running as an in-cluster Pod — this
doesn't actually work**; see "Known limitations" below for why. Without
`network_links`, no extra infrastructure beyond the cluster itself is needed.

---

## Bootstrap from scratch

Everything needed to go from nothing installed to a MicroK8s cluster with
Grafana + Prometheus running, ready for this project's own controller to be
deployed onto it. This section covers only the generic cluster + monitoring
infrastructure — the emulator's own images, controller, and PodMonitors are
covered right after in "Build & push images" and "Deploy". If MicroK8s and
the monitoring stack are already running, skip ahead to "Build & push images".

### 1. Launch the control-plane VM

```bash
multipass launch 22.04 --name microk8s-vm --cpus 4 --memory 8G --disk 48G
```

### 2. Install MicroK8s on it

```bash
multipass exec microk8s-vm -- sudo snap install microk8s --classic --channel=1.32/stable
multipass exec microk8s-vm -- sudo usermod -a -G microk8s ubuntu
multipass exec microk8s-vm -- microk8s status --wait-ready
```

### 3. Enable the addons this project needs

```bash
multipass exec microk8s-vm -- microk8s enable dashboard dns ha-cluster helm helm3 hostpath-storage metrics-server rbac storage
```

`rbac` matters in particular: `manifests/controller.yaml`'s `Role`/`ClusterRole`
grants only actually *restrict* anything once RBAC enforcement is switched on.

### 4. Point standalone `kubectl`/`helm` at the cluster

Every other command in this runbook (and the standalone `helm` install below)
assumes a working default kubeconfig — copy the cluster's out once:

```bash
multipass exec microk8s-vm -- microk8s config > ~/.kube/config
```

(If you already have other clusters in `~/.kube/config`, merge instead of
overwriting — see the `kubectl config` docs.) Verify:

```bash
kubectl get nodes
# NAME           STATUS   ROLES    AGE   VERSION
# microk8s-vm    Ready    <none>   ...   v1.32.13
```

### 5. Install the monitoring stack (Prometheus + Grafana)

Via `kube-prometheus-stack`, using the values overlay in
`manifests/monitoring-values.yaml` (pins Grafana to this VM with durable
storage, exposes both on stable NodePorts — see "Endpoints & credentials"
below). Run from the **repo root**, using the standalone `helm` from step 4:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kps prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --version 86.2.0 \
  -f manifests/monitoring-values.yaml
```

`kps` is the release name every other command and manifest in this repo
assumes (`kps-grafana` secret, `release: kps` PodMonitor label selector,
etc.) — don't rename it without also updating those. Wait for it to settle:

```bash
kubectl get pods -n monitoring --watch
# Ctrl-C once everything is Running/Completed
```

You now have a bare cluster with monitoring but nothing from this project
deployed yet — continue to "Build & push images" and "Deploy" below.

---

## Build & push images

**Important:** the controller's Dockerfile copies a manifest file from
`manifests/`, so it must be built from the **repo root** with `-f`. The
worker has no such constraint.

```bash
# Worker
docker build -t jp36/emulator-worker:latest worker/
docker push jp36/emulator-worker:latest

# Controller — build context = repo root
docker build -f controller/Dockerfile -t jp36/emulator-controller:latest .
docker push jp36/emulator-controller:latest
```

If the COPY layer is cached and you suspect file changes weren't picked
up, force a fresh build:

```bash
docker build --no-cache -t jp36/emulator-worker:latest worker/
```

---

## Deploy

### First time

```bash
kubectl apply -f manifests/controller.yaml
kubectl apply -f manifests/monitoring.yaml
```

> Do **not** `kubectl apply -f manifests/worker-template.yaml`. It's a
> stencil with `__TEMPLATE__` / `__ROLE__` placeholders; the controller
> reads it at runtime and substitutes per app (`__ROLE__` ← the app's name).

### Load the Grafana dashboards

The kube-prometheus-stack Grafana sidecar auto-loads any ConfigMap labelled
`grafana_dashboard=1` in the `monitoring` namespace (see
`manifests/monitoring-values.yaml`). Nothing generates these automatically —
create one ConfigMap per file in `grafana/`:

```bash
for f in grafana/*.json; do
  name="grafana-dash-$(basename "$f" .json)"
  kubectl create configmap "$name" \
    --from-file="$(basename "$f")=$f" \
    -n monitoring --dry-run=client -o yaml \
    | kubectl label --local -f - grafana_dashboard=1 -o yaml \
    | kubectl apply -f -
done
```

The sidecar picks up new/changed ConfigMaps within about a minute — no
Grafana restart needed. Re-run this any time you edit a dashboard JSON file;
`kubectl apply` updates the existing ConfigMap in place.

### After rebuilding the controller image

The controller is a `Pod` (not a Deployment), so it doesn't auto-restart.
Delete + re-apply to force a pull of the new image:

```bash
kubectl delete pod controller
kubectl apply -f manifests/controller.yaml
kubectl wait --for=condition=Ready pod/controller --timeout=120s
```

### After rebuilding the worker image

Workers run inside Deployments managed by the materialiser. The simplest
way to force a pull is to scale, or trigger a rollout:

```bash
kubectl rollout restart deployment -l template=<name>
```

Or tear down + re-POST the template (clean slate):

```bash
curl -X DELETE http://192.168.2.2:30081/template
# re-POST
```

---

## Endpoints & credentials

Everything below assumes the NodePort Services from `manifests/controller.yaml`
and the `kps` monitoring stack (see "Bootstrap from scratch") are both up.
Replace `192.168.2.2` with your MicroK8s host IP throughout.

| Service | URL | Username | Password / token |
|---|---|---|---|
| Controller API | `http://192.168.2.2:30081` | — | none — no authentication (see caveat below) |
| Grafana | `http://192.168.2.2:30300` | `admin` | `kubectl get secret kps-grafana -n monitoring -o jsonpath='{.data.admin-password}' \| base64 -d` |
| Prometheus | `http://192.168.2.2:30090` | — | none — no authentication |
| Kubernetes Dashboard | `https://192.168.2.2:30443` | — | bearer token, see below |

### Controller API

`http://192.168.2.2:30081` — plain HTTP, no login. `GET /docs` is a dark-mode
Swagger UI if you want to browse the API in a browser; every route is also
in `API.md`. Since there's no authentication at all, don't expose this
NodePort beyond a trusted network — anyone who can reach it can materialise,
patch, or delete your topology.

### Grafana

`http://192.168.2.2:30300`. Username is always `admin`; the password is
auto-generated by the Helm chart and stored in a Secret, not written down
anywhere in this repo (don't hardcode it into docs or commit it — it's a
live credential). Fetch it on demand:

```bash
kubectl get secret kps-grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d
echo
```

Relevant dashboards (auto-provisioned via the `grafana_dashboard=1` labelled
ConfigMap sidecar — see `grafana/`): a pod-agnostic overview (whose bottom
section also shows a `calibration/calibrate.py` run *live*, for side-by-side
comparison — see `calibration/README.md`), the per-link latency/bandwidth
dashboard (configured vs. measured), the topology Node Graph backed by
`GET /graph`, and the Model-calibration dashboard that visualises a
*completed* `calibrate.py` run.

### Prometheus

`http://192.168.2.2:30090`. No login. Useful for running raw PromQL against
`worker_*` and `emulator_configured_*` metrics directly (Grafana's Explore
view works too, using the pre-wired datasource).

### Kubernetes Dashboard

`https://192.168.2.2:30443` — **HTTPS with a self-signed cert**, so your
browser will show a warning; click through it (e.g. "Advanced → Proceed").
Login is a bearer token, not a username/password. Two options depending on
what you need:

```bash
# The addon's own default token (Service Account "default" in kube-system).
# No RBAC bound to it — logs in fine, but most resource views show
# "Forbidden" since it has no permissions.
kubectl get secret -n kube-system microk8s-dashboard-token -o jsonpath='{.data.token}' | base64 -d
echo

# A cluster-admin token, for actually browsing/managing real resources —
# short-lived (1h by default), mint a fresh one whenever you need it:
kubectl create token dashboard-admin -n kube-system
```

Whichever you use, treat it as a live credential — same caution as the
Grafana password.

---

## Multi-node setup (MicroK8s + Multipass)

"Bootstrap from scratch" above gets you one node (the control-plane VM,
`microk8s-vm`) — fine for testing `placements`/`x` propagation/scaling, but
the whole point of `network_links` is to see shaping across real separate
nodes. This adds worker VMs to run a template like
`templates/cdn-live-video.json`, which declares 4 sites via its
`node_site_mapping` — all on **dedicated** VMs, none of them the
control-plane:

| Site | Node (new VM) | Pods hosted |
|---|---|---|
| site-A | `site-a` | camera-a, camera-b, transcoder×2, hls-packager |
| site-B | `site-b` | edge-cache |
| site-C | `site-c` | edge-cache |
| site-D | `site-d` | video-player |

Adjust VM count/names to match whatever sites your own template's
`node_site_mapping` declares. No node labelling is needed anywhere in this
flow — the site → node mapping lives entirely in the template's
`node_site_mapping` field, and the controller pins pods directly by node
hostname.

### 1. Launch the worker VMs

```bash
multipass launch 22.04 --name site-a --cpus 2 --memory 3G --disk 20G
multipass launch 22.04 --name site-b --cpus 2 --memory 3G --disk 20G
multipass launch 22.04 --name site-c --cpus 2 --memory 3G --disk 20G
multipass launch 22.04 --name site-d --cpus 2 --memory 3G --disk 20G
```

### 2. Install MicroK8s on each (match your control-plane's channel)

```bash
for vm in site-a site-b site-c site-d; do
  multipass exec "$vm" -- sudo snap install microk8s --classic --channel=1.32/stable
  multipass exec "$vm" -- sudo usermod -a -G microk8s ubuntu
done
```

### 3. Join them to the cluster as workers

Generate one join token on the control-plane VM (default TTL is reusable
for multiple joins, not one-shot):

```bash
multipass exec microk8s-vm -- microk8s add-node
```

Copy the printed `microk8s join <ip>:<port>/<token>` line and run it **with
`--worker`** on each new VM:

```bash
multipass exec site-a -- sudo microk8s join <paste-connection-string> --worker
multipass exec site-b -- sudo microk8s join <paste-connection-string> --worker
multipass exec site-c -- sudo microk8s join <paste-connection-string> --worker
multipass exec site-d -- sudo microk8s join <paste-connection-string> --worker
```

You'll see a `hostpath-storage... not suitable for multi node clusters`
warning — harmless here since only Grafana uses it, and Grafana stays
pinned to the control-plane node (`manifests/monitoring-values.yaml`).

### 4. Verify

```bash
kubectl get nodes -o wide
```

Expect all 5 nodes `Ready`: `microk8s-vm` plus `site-a`, `site-b`, `site-c`,
`site-d`.

### 5. Apply the node-read RBAC grant

`netem.py` reads `/api/v1/nodes` to resolve each `node_site_mapping` entry's
node name to its InternalIP for `tc` shaping — this needs a `ClusterRole`
(nodes are cluster-scoped; a namespaced `Role`'s rules for them are
ignored). `manifests/controller.yaml` already declares
`controller-emulator-nodes` for this:

```bash
kubectl apply -f manifests/controller.yaml
```

(Remember: as covered in "Known limitations", this RBAC grant lets
`netem.py` *discover* node IPs, but the actual `tc` shaping via
`multipass exec` still won't run from inside the containerised controller —
this step is still worth doing since it's needed either way, but don't
expect measured latency/bandwidth to move until that's addressed.)

### 6. Turn off the single-node fallback

Edit `manifests/controller.yaml` — blank the `DEFAULT_NODE` value:

```yaml
        - name: DEFAULT_NODE
          value: ""
```

Then restart it — the controller is a bare `Pod` (not a Deployment), so
`kubectl apply` alone won't pick up an env-var change on the already-running
Pod; delete it first so the re-apply recreates it fresh:

```bash
kubectl delete pod controller
kubectl apply -f manifests/controller.yaml
kubectl wait --for=condition=Ready pod/controller --timeout=120s
```

### 7. Confirm the template's node_site_mapping matches your VM names

`templates/cdn-live-video.json` already declares:

```json
"node_site_mapping": [
  { "k8s_node": "site-a", "site_name": "site-A" },
  { "k8s_node": "site-b", "site_name": "site-B" },
  { "k8s_node": "site-c", "site_name": "site-C" },
  { "k8s_node": "site-d", "site_name": "site-D" }
]
```

If you named your VMs differently, edit `k8s_node` to match — it must be
the exact k8s node name (`kubectl get nodes` — normally the same as the
Multipass VM name).

### 8. Re-POST the template

Re-POSTing the same template name re-materialises idempotently and picks
up the resolved node pins — Kubernetes rolls each Deployment's pods onto
the newly matching nodes:

```bash
curl -X POST http://192.168.2.2:30081/template \
  -H 'Content-Type: application/json' \
  -d @templates/cdn-live-video.json
```

A malformed or unresolvable site name (a `placements`/`network_links` entry
with no matching `node_site_mapping` entry) 400s here immediately, rather
than leaving pods stuck `Pending`.

### 9. Verify placement

```bash
kubectl get pods -o wide
```

Each pod's `NODE` column should match the site/node table above.

### 10. Check the dashboard

Give pods ~30–60s to settle, then open the **Emulator — Network** Grafana
dashboard (see "Endpoints & credentials" above). Set the `base_ms` control
to `0` first — the real unshaped baseline between separate Multipass VMs
isn't the same as same-node loopback, so check the raw measured number
before assuming a baseline to subtract. **Given the `multipass exec`
limitation above, expect the measured panels to stay flat at the unshaped
baseline rather than converging toward the configured `network_links`
values** — the *configured* panels will still show correctly, since those
are declarative and don't depend on `tc` actually running.

---

## Drive the system

A controller manages **one** template, so the routes are singular and take
no name. POSTing a different name while one exists returns `409` — DELETE
or PATCH the existing one first.

```bash
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

# Inspect (or load a ready-made one from templates/*.json)
curl -s http://192.168.2.2:30081/template | python3 -m json.tool

# Tear down
curl -X DELETE http://192.168.2.2:30081/template
```

The response from POST returns the resolved peer map (Service names) and
the app list; verify it matches what you intended before walking away.

---

## Query & scale via the observability endpoints

These are the place to *observe everything* — clean JSON, every response
tagged with this VM's `site` block (`SITE_ID` / `SITE_TIER` — this
controller's own federation identity, unrelated to a template's
`node_site_mapping` site names used above) and a generation `timestamp` in
the controller's local timezone. Full schemas in `API.md`; the day-to-day
commands:

```bash
# What is running on this VM right now? (also the liveness "ok": true)
curl -s http://192.168.2.2:30081/overview | python3 -m json.tool

# Live status: per-app desired/ready (under a `roles` key), resolved x,
# target vs actual sums, per-pod gauges, measured edge traffic.
curl -s http://192.168.2.2:30081/measurements/now | python3 -m json.tool

# CPU/RAM/net aggregated over a time window (defaults to the last 15m).
curl -s "http://192.168.2.2:30081/measurements/range" | python3 -m json.tool
# A fixed past hour, CPU only (timestamps are controller-local; no offset = local):
curl -s "http://192.168.2.2:30081/measurements/range?start=2026-06-10T11:00:00&end=2026-06-10T12:00:00&resources=cpu" \
  | python3 -m json.tool
# The last 6 ten-minute periods (the last hour), each aggregated separately:
curl -s "http://192.168.2.2:30081/measurements/periods?count=6&chunk=10m" \
  | python3 -m json.tool
```

Scaling an app adds/removes pods *and* re-wires the topology (re-resolves
x, re-assigns peers + iperf port offsets) — it is not a bare Deployment
replica bump. There's no dedicated scale endpoint; PATCH the app's count:

```bash
# Set backend to 4 pods
curl -X PATCH http://192.168.2.2:30081/template \
  -H 'Content-Type: application/json' -d '{"apps.backend.count": 4}'
```

After a scale-up, `/measurements/now` shows new pods with empty
`metrics: {}` until they become Ready and land in Endpoints — watch them
fill in there.

> The windowed `/measurements/range` and `/periods` need Prometheus
> reachable at `PROM_URL`. If it isn't, they still return 200 with
> `prometheus.available: false` and empty `totals` — overview /
> measurements/now (live-scrape based) keep working regardless.

---

## Template authoring — math that conserves

Network targets across an edge **must conserve traffic** or the actuals
won't match the configured numbers. If frontend sends 7 Mbps total and
backends each want 2 Mbps, that's only 4 Mbps of demand — the remaining
3 Mbps still lands on the backends, making each one ~75% over target.

Rule of thumb for a single `from → to` edge:

```
sum(from_app.net  ×  from_app.count)
   =
sum(to_app.net    ×  to_app.count)
```

For 1 frontend → 2 backends with each backend at 2 Mbps:
- frontend.net should resolve to `2 × 2 = 4 Mbps`
- formulas: `frontend.net = {a:0.2, b:2}` at `x=10` → 4 ✓

CPU and RAM don't need to conserve — they're independent per app.

---

## Tuning the iperf3 server pool

Each templated worker pod runs N iperf3 servers on consecutive ports
starting at 9999. N is set by the `IPERF_PORT_COUNT` env var, explicitly
set to `"2"` in `manifests/worker-template.yaml` (the code's own fallback in
`worker/state.py` is `8` if the env var were ever unset entirely — the
shipped manifest always sets it, so `2` is what you actually get).

- **N=2** — fits 1-to-many fan-out. ~2.4 MiB RAM baseline for the pool.
- **N=8** — fits many-to-many topologies up to 8 concurrent inbound
  source pods on a single target. ~10 MiB RAM baseline.
- Larger N tolerates more concurrency but raises the RAM floor. For
  apps with low RAM targets (≤30 MB), the baseline can exceed the
  target and the RAM nudger won't be able to converge.

To change it: edit the env value in `manifests/worker-template.yaml`,
rebuild the controller image (the manifest is baked in), redeploy the
controller pod, then re-POST or re-apply each template so new pods come
up with the new env.

---

## Observe — proven verification commands

These are the exact checks used during system validation.

### Confirm the controller is on the latest code

```bash
kubectl logs controller --tail=20
# Look for:
#   Controller started
#   Controller listening on 0.0.0.0:8081
```

### Inspect a materialised template

```bash
kubectl get all,configmap -l template=<name>
curl -s http://192.168.2.2:30081/template | python3 -m json.tool
```

The GET endpoint reconstructs the template from cluster annotations —
this is how you verify the materialiser wrote the right metadata.

### Frontend's peer IPs (post Phase 2)

```bash
kubectl get configmap wt-<name>-<app>-config \
  -o jsonpath='{.data.config\.json}' | python3 -m json.tool
```

The `peers` field should be a list of concrete pod IPs (not the Service
name). If it's still a Service name like `wt-fb-backend`, Phase 2 hasn't
completed yet (it has a 30s endpoint-readiness timeout).

### Per-pod metrics (truth comparison)

```bash
kubectl top pod -l template=<name>

for pod in $(kubectl get pods -l template=<name> -o name); do
  echo "--- $pod ---"
  pf_port=$(( 8000 + RANDOM % 1000 ))
  kubectl port-forward $pod ${pf_port}:8080 >/dev/null 2>&1 &
  PF=$!
  sleep 2
  curl -s http://localhost:${pf_port}/metrics \
    | grep -E "^worker_(actual|target)_(cpu|ram|net)|^worker_cpu_stress"
  kill $PF 2>/dev/null
  wait $PF 2>/dev/null
done
```

What "good" looks like after ~60s of settle time:
- RAM actual within ~1% of target
- Net actual within ~5% of target on every pod (senders and receivers)
- CPU `kubectl top` near target; `/metrics` may be bursty but rate over
  `[1m]` in Grafana converges. `worker_actual_cpu_millicores` settles within
  the CPU deadband (`max(CPU_TOLERANCE_MC, 5% of target)`) of target ~30-45s
  after a (re)configure. Watch `worker_cpu_stress_millicores` step toward its
  resting value over the first few `CPU_ADJUST_INTERVAL_S` ticks — that's the
  feedback loop converging. Worker logs print a `CPU nudge: …` line on each
  resize.

### Worker startup log

```bash
kubectl logs deployment/wt-<name>-<app> | head -30
# Look for:
#   spawn: iperf3 -s -p 9999          (and one per IPERF_PORT_COUNT)
#   Configuring x=… peers=[…]         (Phase 2 reconfigure)
#   Sampler heartbeat                 (every 30s)
```

---

## Diagnostics — common failures and fixes

### Controller returns "Connection refused"

The controller is a `Pod` (not a Deployment); manual `kubectl delete`
doesn't self-heal. Re-apply:

```bash
kubectl apply -f manifests/controller.yaml
```

### Pods stuck in `ContainerCreating`

```bash
kubectl describe pod <pod-name>
```

Usually `ImagePullBackOff`. Confirm the image was pushed and the tag
matches `manifests/worker-template.yaml`'s `__IMAGE__` substitution
(which defaults to `jp36/emulator-worker:latest`).

### `kubectl exec POD -- COMMAND` fails

The `microk8s kubectl` wrapper sometimes consumes the `--` separator. Workaround:

```bash
# Use port-forward + curl instead of exec for inspection:
kubectl port-forward <pod> 8080:8080 &
curl localhost:8080/status

# Or copy files out:
kubectl cp <pod>:/etc/emulator/config.json /tmp/config.json
```

### POST /template returns 502

A k8s API call failed mid-materialisation. Resources created up to the
failure point still exist. Re-POST is idempotent (`_apply` falls back to
PATCH on 409). Or `DELETE` the template to fully clean up.

### Backends show 0 Mbps actual net

Phase 2 hasn't completed yet (the workers are still in their initial
"empty peers" configure). Wait ~30s after POST and re-check. If it's
persistent: check controller logs for `Materialising template <name>`
and `wrote N peer IPs into …`.

### Worker CPU at full pod limit (e.g. 1000m) when target is much lower

Stress-ng can occasionally over-shoot during a burst. `kubectl top` over
a longer window will average out. If it's sustained, check the workers'
`/metrics` `worker_actual_cpu_millicores` against `worker_target_cpu_millicores`
— if both are pegged, the formula is producing a higher target than
intended.

### Grafana panels are empty for new templated pods

The PodMonitors in `manifests/monitoring.yaml` must be applied:

```bash
kubectl apply -f manifests/monitoring.yaml
kubectl get podmonitor      # expect: worker-templates AND emulator-controller
```

---

## Cleanup

### Tear down everything materialised

```bash
# The materialised template (one per controller)
curl -X DELETE http://192.168.2.2:30081/template

# Belt-and-braces: anything still labelled as ours
kubectl delete all,configmap -l app.kubernetes.io/managed-by=emulator-controller
```

### Tear down the static infrastructure

```bash
kubectl delete -f manifests/controller.yaml
kubectl delete -f manifests/monitoring.yaml
```

---

## Known limitations

- **Controller is a `Pod`** — convert to a Deployment for self-healing.
- **stress-ng CPU is approximate, but now closed-loop** — `--cpu-load`
  accuracy depends on scheduler responsiveness, so actual CPU is somewhat
  bursty. CPU sizing is no longer open-loop: `configure()` only *seeds*
  stress-ng directly at the raw target — no baseline is measured there at
  all (see `worker/worker.py`'s `configure()` docstring) — then
  `_adjust_cpu` (every `CPU_ADJUST_INTERVAL_S`, default 15 s) re-reads the
  pod's total cgroup CPU and resizes stress-ng so total converges on target.
  A seed that overshoots during startup churn — which previously baked in a
  permanently under-target pod — now self-corrects within a few steps. Residual error
  settles inside the deadband (`max(CPU_TOLERANCE_MC, CPU_TOLERANCE_FRAC ×
  target)`); tighten `CPU_TOLERANCE_MC`/`CPU_TOLERANCE_FRAC` for a closer hold
  or lower `CPU_GAIN` if you see it hunting. One case the loop cannot fix: if
  the iperf3+python baseline alone exceeds a (low) target, stress-ng goes to 0
  and the pod still reads above target — fix the formula, not the worker. The
  worker passes `--cpu-load-slice` (env
  `CPU_LOAD_SLICE_MS`, default 40ms — both the code fallback in
  `worker/state.py` and the explicit value in `manifests/worker-template.yaml`
  agree) to break the duty cycle into fine slices, which smooths it and
  tightens accuracy under contention. 40ms is a balance of smoothness and
  mean accuracy; lower it (e.g. 20) for smoother still at a slightly larger
  under-bias, raise it towards stress-ng's coarse default (`0`) for less
  overhead. For
  steady-state numbers prefer the windowed `/measurements/range` or Grafana
  `rate(…[1m])` over single-second snapshots. Note the target tracks CPU
  *time* (cgroup `usage_usec`), so CPU-frequency scaling doesn't skew it.
  If a node is oversubscribed (sum of CPU targets > node cores), no knob
  can make pods hit target — check `kubectl top node`.
- **Endpoint enumeration is one-shot.** Phase 2 reads pod IPs at
  materialise time. If backend pods restart (e.g. rollout, eviction),
  their new IPs aren't propagated — re-POST or re-apply the template.
- **`network_links` shaping requires `multipass` reachable from wherever the
  controller process runs.** `netem.py` shapes links by `multipass exec`-ing
  into each site's node — this works when the controller runs as a plain
  process on the Multipass host, but the shipped containerized deployment
  (`manifests/controller.yaml`, a Pod inside the cluster) has **no path to
  the host's Multipass hypervisor** — no `multipass` binary in the image, no
  `hostPath`/privileged access, no socket mount. In that deployment,
  `netem.apply()`/`teardown()` silently no-op (`multipass not found` warning
  in the controller logs) — pods still land on the right sites via
  `placements`/`node_site_mapping`, but `tc netem`/`htb` shaping never
  actually applies. `emulator_configured_rtt_ms`/`_bandwidth_mbps` still
  publish the *declared* values regardless, so a dashboard comparing
  configured vs. measured will show the measured side never converging.
  Fixing this needs a deployment change (run the controller as a host
  process instead of a Pod, or give the container real access to the host's
  Multipass control path) — not something the current manifests provide.
- **Template formulas must conserve traffic** (see "Template authoring"
  above). The materialiser doesn't validate conservation.
- **`kubectl exec --`** is broken by the wrapper. Use
  port-forward or `kubectl cp` for in-pod inspection.

---

## Reference: file layout

```
controller/
  app.py            FastAPI HTTP routes: /template CRUD + /graph + observability
  api.py            observability: overview + measurements (k8s + scrape + prom)
  linkspec.py       network_links shape validation + configured metrics
  netem.py          tc netem/htb link shaping via multipass exec
  runner.py         scenario runner — steps x through runtime_scenarios
  watcher.py        ConfigMap reconciler for declarative (non-HTTP) templates
  prom.py           Prometheus HTTP API client (instant queries), graceful
  graph.py          topology scrape + edge measurement (shared by /graph + api)
  materialiser.py   template → resources, two-phase create + IP resolve
  k8s.py            shared k8s API client
  Dockerfile        builds from REPO ROOT (needs manifests/)
worker/
  worker.py         entrypoint, configure() funnel
  state.py          STATE dict, POD_NAME, IPERF_PORT_COUNT, CPU_LOAD_SLICE_MS
  loads.py          stress-ng/mmap/iperf3 + per-peer supervisor threads
  metrics.py        cgroup-CPU sampler, prometheus gauges, RAM nudger
  watcher.py        filesystem watchdog on the mounted ConfigMap
manifests/
  controller.yaml         RBAC + ServiceAccount + Pod + NodePort Service
  worker-template.yaml    stencil baked into the controller image
  monitoring.yaml         PodMonitor for the templated worker pods + controller
  monitoring-values.yaml  kube-prometheus-stack Helm values overlay
grafana/
  grafana-dashboard.json  pod-agnostic dashboard
  grafana-network.json    per-link latency + bandwidth (configured vs measured)
  grafana-nodegraph.json  Node Graph panel backed by GET /graph
```
