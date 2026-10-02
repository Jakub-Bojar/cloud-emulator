"""
Controller pod — HTTP surface (FastAPI).

Front door for the emulation. Materialises templated multi-worker
topologies: POST /templates validates the JSON, then the materialiser
creates one Deployment + ConfigMap + Service per role declared in the
template and writes each role's load formulas + peer list into its
ConfigMap. DELETE /templates/<name> tears the whole topology down.

This module is only the web layer: routing, request-shape validation,
error mapping, and concurrency control. All behaviour lives in the
materialiser / graph / api / prom modules, unchanged.

Why FastAPI (and not the stdlib HTTPServer): sync `def` endpoints run in
an anyio threadpool, so a slow blocking call (materialise waiting on
Endpoints, per-pod scrapes, Prometheus queries) no longer blocks the
liveness probe on /health. The single-threaded HTTPServer used to
serialise everything, so a long materialise could starve /health and get
the pod killed mid-operation.

One controller manages exactly ONE template (this site's topology), so the
template routes are singular and take no name — the template's `name` field
still exists internally because the k8s resource names are derived from it.

Endpoints
---------
POST   /template           materialise the posted template. Re-POSTing the
                           same name re-materialises idempotently; 409 if a
                           different template is already materialised;
                           400 if it names a node the cluster doesn't have.
                           POST and PATCH responses carry `warnings` (pods
                           not Ready in time, links not shaped) and
                           `network_shaping` (per-link tc outcome)
GET    /template           the materialised template, in full (404 if none)
PATCH  /template           partial update — merge, re-resolve, re-materialise.
                           Change anything: x, an app's cpu/ram/net/count/tier,
                           edges, sites, or network_links. Accepts nested JSON or
                           dot-path shorthand, e.g. {"x": 80,
                           "apps.ingest.cpu.a": 5}
DELETE /template           tear down the materialised template
GET    /graph              Grafana Node Graph payload (nodes + measured edges);
                           ?view=pods for per-pod nodes (default: per-role)

Observability (see api.py / API.md)
-----------------------------------
GET    /overview               site-wide snapshot; subsumes the old /health
                               ("ok": true). The k8s readiness probe uses a
                               TCP-socket check instead (see controller.yaml).
                               Includes `network_shaping` as of the last
                               materialise
GET    /measurements/now       fused k8s + live worker metrics, instantaneous
GET    /measurements/range     CPU/RAM/net aggregated between ?start and ?end
                               (ISO 8601 or unix; default: the last 15 min)
GET    /measurements/periods   the last ?count chunks of ?chunk each, ending
                               now, each aggregated separately


Interactive docs (generated from the code, no hand-maintained spec):
GET    /docs   and   GET /openapi.json
"""

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import api
import graph
import linkspec
import materialiser
import netem
import runner
import watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [controller] %(message)s",
)
log = logging.getLogger(__name__)

# Allowed CORS origin for browser clients (Swagger UI, Grafana, etc.). "*"
# is fine for a dev/research tool on a trusted network; set CORS_ALLOW_ORIGIN
# to a specific origin to lock it down.
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")


# ── Write lock ──────────────────────────────────────────────────────────────
# Endpoints run concurrently in the threadpool, so two writes could
# interleave (POST racing PATCH, double POST). One controller manages one
# template, so a single lock serialises all writers; reads don't take it.
_write_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Declarative ingestion: poll labelled ConfigMaps and reconcile them
    # via the same materialiser used by POST /templates. Same daemon thread
    # the old controller started in main(). Keep the server at ONE process
    # (uvicorn --workers 1) so only one reconciler runs.
    watcher.start()
    # Resume the scenario runner if a template with runtime_scenarios is already
    # materialised (e.g. the controller pod restarted). The scenario clock is
    # wall-clock from runner start and not persisted, so it restarts from now.
    try:
        names = materialiser.list_managed()
        if names:
            tmpl = (materialiser.get_managed(names[0]) or {}).get("template")
            if tmpl:
                runner.start(tmpl, names[0])
    except Exception:
        log.exception("could not resume scenario runner at startup")
    log.info("Controller started")
    yield
    runner.stop()


app = FastAPI(
    title="Cloud-Native Emulator Controller",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,  # served by the dark-mode /docs route below
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ── Dark-mode Swagger UI ─────────────────────────────────────────────────────
# Swagger UI has no built-in dark theme, so we serve FastAPI's stock /docs
# page with an inversion filter appended: flip the whole UI to dark, then
# flip syntax-highlighted code blocks back so they keep their own colours.
# hue-rotate(180deg) restores the original hues (greens stay green, etc.).
_DARK_CSS = """<style>
  body { background-color: #0f1217; }
  .swagger-ui { filter: invert(88%) hue-rotate(180deg); }
  .swagger-ui .microlight { filter: invert(100%) hue-rotate(180deg); }
</style>"""


@app.get("/docs", include_in_schema=False)
def swagger_docs() -> HTMLResponse:
    html = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} — docs",
    ).body.decode()
    return HTMLResponse(html.replace("</head>", _DARK_CSS + "</head>"))


# ── Error mapping ───────────────────────────────────────────────────────────
# Replaces the per-handler try/except ladders. The materialiser raises
# ValueError for client-side problems (bad field, cycle, unknown resource) and
# RuntimeError when a k8s API call fails.
@app.exception_handler(ValueError)
async def _value_error(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(RuntimeError)
async def _runtime_error(request: Request, exc: RuntimeError):
    log.exception("k8s API failure on %s", request.url.path)
    return JSONResponse(status_code=502, content={"error": str(exc)})


# ── Request models ──────────────────────────────────────────────────────────
# Pydantic validates the *shape* and auto-documents it at /docs. The richer
# graph-level checks (cycle detection, edge endpoints exist) stay in
# materialiser.validate(), called explicitly below. extra="allow" preserves
# any unknown fields so the stored template annotation round-trips intact,
# matching the permissive behaviour of the old hand-rolled validator.
class Axis(BaseModel):
    model_config = ConfigDict(extra="allow")
    a: float
    b: float


class PlacementNode(BaseModel):
    model_config = ConfigDict(extra="allow")
    node: str
    count: int = Field(ge=1)


class Placement(BaseModel):
    model_config = ConfigDict(extra="allow")
    on: dict[str, str] | None = Field(
        None,
        description="Schedule the app's pods on nodes matching these labels "
                    "(nodeSelector). Use the labels your nodes already carry.",
        examples=[{"tier": "edge"}],
    )
    spread: bool | None = Field(
        None,
        description="With `on`: spread the replicas evenly across the matching "
                    "nodes (topologySpreadConstraints).",
    )
    node: str | None = Field(
        None, description="Pin all of the app's pods to this one node (hostname).")
    nodes: list[PlacementNode] | None = Field(
        None,
        description="Split the app's pods across nodes: a list of "
                    '{"node", "count"}. The controller materialises one '
                    "Deployment per node (all sharing the app's Service), so "
                    "this is the way to run several of the same pod on different "
                    "nodes. The counts must sum to the app's `count`.",
        examples=[[{"node": "edge-1", "count": 2}, {"node": "edge-2", "count": 3}]],
    )


class PlacementEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    site: str | None = Field(
        None,
        description="A site name from the template's `node_site_mapping`. "
                    "Use this or `sites`.")
    sites: list[str] | None = Field(
        None,
        description="List of site names from `node_site_mapping` — one "
                    "Deployment per site, each with `count` replicas. Use "
                    "this or `site`.")
    count: int = Field(ge=1, description="Pods at this site (or per site when using `sites`).")


class NodeSiteMapping(BaseModel):
    model_config = ConfigDict(extra="allow")
    k8s_node: str = Field(description="The real Kubernetes node name (hostname).")
    site_name: str = Field(
        description="The logical site name used to refer to this node "
                    "elsewhere in the template (`placements`, `network_links`). "
                    "Must be unique; the same k8s_node may back several "
                    "site_names.")


class App(BaseModel):
    model_config = ConfigDict(extra="allow")
    count: int | None = Field(
        None,
        ge=1,
        description="Total pod count. Required when using `placement`. "
                    "Optional when using `placements` — derived automatically "
                    "from the sum of placement entry counts.",
    )
    cpu: Axis
    ram: Axis
    net: Axis
    placement: Placement | None = Field(
        None,
        description="Legacy placement object. One of: "
                    '{"on": {<label>: <value>}, "spread"?}; '
                    '{"node": "<hostname>"}; '
                    'or {"nodes": [{"node", "count"}]}.',
        examples=[{"on": {"tier": "edge"}, "spread": True}],
    )
    placements: list[PlacementEntry] | None = Field(
        None,
        description="Site placement list. Each site name must appear in the "
                    "template's top-level `node_site_mapping` — no node "
                    "labelling needed, the controller pins pods directly by "
                    "hostname. One Deployment per entry (per site, for "
                    "`sites`). Top-level `count` is optional — derived from "
                    "the sum.",
        examples=[[
            {"site": "site-A", "count": 1},
            {"sites": ["site-B", "site-C"], "count": 1},
        ]],
    )


class Edge(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    from_: str = Field(alias="from")
    to: str


class NetworkLink(BaseModel):
    model_config = ConfigDict(extra="allow")
    from_: str = Field(
        alias="from",
        description="Source site name. Must appear in `node_site_mapping` "
                    "when the template declares one.")
    to: str = Field(
        description="Target site name. Must appear in `node_site_mapping` "
                    "when the template declares one.")
    rtt_ms: float = Field(
        gt=0,
        description="Round-trip latency in milliseconds. netem applies rtt_ms/2 "
                    "as one-way delay on each side.")
    bandwidth_mbps: float | None = Field(
        None, gt=0,
        description="Optional per-direction bandwidth cap in Mbps.")


class Template(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    x: float | dict[str, float] = Field(
        0,
        description="The load signal that drives the topology. Either a single "
                    "number — every source app (one with no inbound edges) "
                    "starts there — or an {app: number} map giving each source "
                    "app its own starting value (sources not listed default to "
                    "0). The map form lets one template run several independent "
                    "sub-systems from different starting points. Downstream apps "
                    "always derive their x from upstream egress, so the map may "
                    "only name source apps.",
        examples=[10, {"camera_a": 80, "camera_b": 10}],
    )
    apps: dict[str, App]
    edges: list[Edge] = []
    node_site_mapping: list[NodeSiteMapping] | None = Field(
        None,
        description="The template's own site → k8s node registry. Each entry "
                    "maps a real Kubernetes node name to a logical site name "
                    "used elsewhere in the template (`placements`, "
                    "`network_links`). Replaces node labelling entirely — the "
                    "controller pins pods by hostname directly. The same "
                    "k8s_node may back several site_names (e.g. testing "
                    "multiple sites on one node).",
        examples=[[
            {"k8s_node": "microk8s-vm", "site_name": "site-A"},
            {"k8s_node": "site-b",      "site_name": "site-B"},
        ]],
    )
    network_links: list[NetworkLink] | None = Field(
        None,
        description="Inter-site network links. The controller applies "
                    "tc netem + htb on each node's NIC so pods inherit the "
                    "latency/bandwidth automatically (no per-pod tracking). "
                    "Site names must match the `site=` node labels used in "
                    "`placements`.",
        examples=[[
            {"from": "site-A", "to": "site-B", "rtt_ms": 30, "bandwidth_mbps": 500},
            {"from": "site-B", "to": "site-C", "rtt_ms": 10},
        ]],
    )
    runtime_scenarios: list[dict] | None = Field(
        None,
        description="Optional time-varying load schedule for the scenario "
                    "runner. A list of phases, each "
                    '{"phase_id", "start_min", "end_min", "x"}, where x is the '
                    "input for that window (minutes since the template was "
                    "POSTed). Each phase's x is a number (every source app) or "
                    "an {app: number} map (per-source starting values), same as "
                    "the top-level x. The controller steps x through the phases "
                    "on a clock; workers hot-reload each change with no pod "
                    "restart. Holds the final phase's x once the schedule "
                    "completes.",
        examples=[[{"phase_id": "warmup", "start_min": 0, "end_min": 5, "x": 10},
                   {"phase_id": "peak", "start_min": 5, "end_min": 15,
                    "x": {"camera_a": 80, "camera_b": 10}}]],
    )


def _stamp(payload: dict) -> dict:
    """Prepend a generation timestamp (controller-local ISO 8601) to a JSON
    response. Every endpoint except /graph returns through this — /graph's
    shape is dictated by the Grafana Node Graph panel, which rejects unknown
    top-level keys."""
    return {"timestamp": api.timestamp(), **payload}


# ── The template: singular CRUD ──────────────────────────────────────────────
def _single_template_name() -> str:
    """Name of the one materialised template. 404 if none. 409 if the cluster
    somehow holds more than one (e.g. legacy state from the old plural API, or
    several labelled ConfigMaps picked up by the watcher) — surfacing that
    beats silently picking one."""
    names = materialiser.list_managed()
    if not names:
        raise HTTPException(404, "no template materialised")
    if len(names) > 1:
        raise HTTPException(
            409, f"multiple templates materialised ({', '.join(names)}); "
                 "tear down the extras first")
    return names[0]


@app.get("/template")
def get_template():
    info = materialiser.get_managed(_single_template_name())
    if info is None or not info.get("template"):
        raise HTTPException(404, "no template materialised")
    return _stamp(info["template"])


@app.post("/template", status_code=201)
def create_template(template: Template):
    # exclude_none keeps the stored template (and GET /template) clean: an app's
    # placement only carries the mode it actually uses (no `on: null, node: null`
    # noise), and absent optionals like runtime_scenarios don't round-trip as null.
    body = template.model_dump(by_alias=True, exclude_none=True)
    # GET /template responses carry an injected `timestamp`; strip it here so
    # re-POSTing a GET round-trips cleanly instead of storing the stamp as
    # template content (extra="allow" would otherwise keep it).
    body.pop("timestamp", None)
    # Cycle detection + edge-endpoint checks beyond Pydantic's shape check.
    materialiser.validate(body)
    with _write_lock:
        # Singleton invariant: re-POSTing the same name re-materialises
        # idempotently; a different name while one exists is a conflict.
        existing = materialiser.list_managed()
        if existing and existing != [body["name"]]:
            raise HTTPException(
                409, f"template {existing[0]!r} is already materialised; "
                     "PATCH it, or DELETE it before posting a new one")
        log.info("Materialising template %s", body.get("name"))
        report = materialiser.materialise(body)
    # (Re)start the scenario runner outside the write lock: a template with
    # runtime_scenarios begins stepping its x on a clock; one without stops any
    # runner left over from a previous template.
    runner.start(body, body["name"])
    # Full materialisation summary: every template section (incl. sites,
    # network_links, per-app tier/node/placement, scenarios) plus the
    # controller-derived resolved x, effective node, and peers — see
    # materialiser.describe.
    # `warnings` lists anything only partly done (pods never Ready, links not
    # shaped); `network_shaping` is the per-link tc outcome. A 201 means the
    # template was accepted and applied, not that every part took effect.
    return _stamp({"name": body["name"], **materialiser.describe(body),
                   **report})


def _normalise_patched(merged: dict) -> dict:
    """Hold a PATCH-merged template to the same schema as a POST body.

    PATCH bodies are free-form (dot-paths, partial objects), so they can't be
    modelled up front — but the merged result can. Without this a PATCH could
    store values POST would reject (e.g. a string coefficient), which reach the
    workers and break them. Returns the dump POST would have stored."""
    try:
        template = Template.model_validate(merged)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors())
        raise ValueError(f"patched template is invalid: {problems}") from exc
    return template.model_dump(by_alias=True, exclude_none=True)


@app.patch("/template")
def patch_template(patch: dict):
    # Body is free-form: supports nested JSON and dot-path shorthand, so it is
    # not modelled. patch_template raises ValueError (→400) / RuntimeError (→502).
    patch.pop("timestamp", None)  # injected by GET /template; not content
    with _write_lock:
        name = _single_template_name()
        result = materialiser.patch_template(name, patch,
                                             normalise=_normalise_patched)
    if result is None:
        raise HTTPException(404, "no template materialised")
    merged, report = result
    # Re-sync the scenario runner to the merged template: edits to
    # runtime_scenarios take effect (and restart the clock from now), and an
    # x-only PATCH that the runner itself made never reaches here.
    runner.start(merged, name)
    return _stamp({
        "name": name,
        "template": merged,
        "peers": materialiser.compute_peers(merged),
        **report,
    })


@app.delete("/template")
def delete_template():
    # _single_template_name 404s when nothing is materialised, so "deleted
    # nothing because it was already gone" stays distinguishable from a real
    # teardown; resolving it under the lock keeps check-then-delete atomic.
    # Stop the scenario runner first so it can't patch x into a template that
    # is being torn down out from under it.
    runner.stop()
    with _write_lock:
        name = _single_template_name()
        log.info("Tearing down template %s", name)
        deleted = materialiser.teardown(name)
    return _stamp({"name": name, "deleted": deleted})


# ── Graph ─────────────────────────────────────────────────────────────────────
@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    # Publish the materialised template's CONFIGURED per-link round-trip latency
    # and bandwidth for Prometheus (the dashboards' "what the link is meant to
    # be" panels). These are declarative — the controller injects nothing — but
    # surfacing them lets a dashboard or another system compare intent against
    # the workers' measured figures. Recomputed each scrape from the live
    # template, so a PATCH retune shows up at once; cleared when there's no
    # template or no network_links.
    rtt_pairs: dict = {}
    bandwidth_pairs: dict = {}
    names = materialiser.list_managed()
    if names:
        template = (materialiser.get_managed(names[0]) or {}).get("template") or {}
        try:
            rtt_pairs, bandwidth_pairs = linkspec.validate_network_links(
                template.get("network_links"))
        except ValueError:
            rtt_pairs, bandwidth_pairs = {}, {}
    linkspec.set_configured_rtt(rtt_pairs)
    linkspec.set_configured_bw(bandwidth_pairs)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/graph")
def get_graph(
    view: str = Query(
        "role",
        description="`role` (default): one node per role, stats summed "
                    "across its pods. `pods`: one node per pod with raw "
                    "pod→pod edges.",
        examples=["pods"],
    ),
):
    """Grafana Node Graph payload (nodes + measured edges) for the
    materialised template. No timestamp — the panel rejects unknown keys."""
    by_pod = view.lower() in ("pod", "pods")
    payload = graph.build_graph(_single_template_name(), by_pod=by_pod)
    if payload is None:
        raise HTTPException(404, "no template materialised")
    return payload


# ── Observability ─────────────────────────────────────────────────────────────
@app.get("/overview")
def overview():
    # "ok" subsumes the old /health endpoint: if this handler runs, the web
    # layer is alive. The rest is the site snapshot from api.overview().
    # network_shaping: whether the template's network_links are actually in
    # effect (as of the last materialise) — see netem.last_result.
    return _stamp({"ok": True, **api.overview(),
                   "network_shaping": netem.last_result()})


@app.get("/measurements/now")
def measurements_now():
    data = api.template_status(_single_template_name())
    if data is None:
        raise HTTPException(404, "no template materialised")
    return _stamp(data)


@app.get("/measurements/range")
def measurements_range(
    start: datetime | None = Query(
        None,
        description="Interval start — ISO 8601 (`2026-06-10T11:00:00`) or "
                    "unix epoch seconds (`1781089200`). A timestamp without "
                    "an offset is the controller's local time (TZ env, e.g. "
                    "Europe/London); `Z` / explicit offsets are honoured. "
                    "Defaults to `end` − 15 minutes.",
        examples=["2026-06-10T11:00:00"],
    ),
    end: datetime | None = Query(
        None,
        description="Interval end, same formats as `start`. Defaults to now.",
        examples=["2026-06-10T12:00:00"],
    ),
    resources: str | None = Query(
        None,
        description="Comma-separated subset of: `cpu`, `ram`, `net`. "
                    "Defaults to all three.",
        examples=["cpu,net"],
    ),
):
    """CPU / RAM / network aggregated between `start` and `end`.

    Per requested resource: template-wide target/actual averages plus actual
    min/max, summed across pods first, with a per-role breakdown and each
    role's resolved input x averaged over the interval. Omit both timestamps
    for the last 15 minutes."""
    res = ([r.strip() for r in resources.split(",") if r.strip()]
           if resources else None)
    # template_range raises ValueError on an unknown resource, start >= end,
    # or an oversized window (→400).
    data = api.template_range(_single_template_name(), start=start, end=end,
                              resources=res)
    if data is None:
        raise HTTPException(404, "no template materialised")
    return _stamp(data)


@app.get("/measurements/periods")
def measurements_periods(
    count: int = Query(
        ...,
        ge=1,
        description="How many chunks to return, counting back from now. e.g. "
                    "`?count=4&chunk=11m` → the last 44 minutes as 4 "
                    "eleven-minute periods.",
        examples=[4],
    ),
    chunk: str = Query(
        ...,
        description="Length of each chunk — e.g. `90s`, `10m`, `1h` (min "
                    "`30s`, the scrape interval).",
        examples=["10m"],
    ),
    resources: str | None = Query(
        None,
        description="Comma-separated subset of: `cpu`, `ram`, `net`. "
                    "Defaults to all three.",
        examples=["cpu,net"],
    ),
):
    """The last `count` chunks of `chunk` each, ending now, aggregated
    separately.

    e.g. `?count=4&chunk=11m` → the last 44 minutes as 4 eleven-minute
    periods. Each period carries the same totals/roles/x blocks as
    `/measurements/range`."""
    res = ([r.strip() for r in resources.split(",") if r.strip()]
           if resources else None)
    # template_periods raises ValueError on a bad chunk/count/resources (→400).
    data = api.template_periods(_single_template_name(),
                                chunk=chunk, count=count, resources=res)
    if data is None:
        raise HTTPException(404, "no template materialised")
    return _stamp(data)
