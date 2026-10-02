"""
Template materialiser.

Takes a parsed template (a dict matching the schema documented in
manifests/worker-template.yaml) and turns it into a set of Kubernetes
resources: one ConfigMap + Deployment + Service per role.

This module is the single source of truth for "what does a template
become in the cluster". Both ingestion paths added in later steps —
the HTTP /templates endpoint and the ConfigMap watcher — call into
materialise() / teardown() here.

Step 2 only adds this module; nothing imports it yet. Step 3 wires it
up to the controller's HTTP handler.
"""

import copy
import json
import logging
import math
import os
import time
from typing import Callable

import yaml

import k8s
import linkspec
import netem

log = logging.getLogger(__name__)

BLUEPRINT_PATH = os.environ.get("BLUEPRINT_PATH",
                                "/etc/emulator/worker-template.yaml")
WORKER_IMAGE = os.environ.get("WORKER_IMAGE",
                              "jp36/emulator-worker:latest")

# Controller-wide fallback node for worker pods. When non-empty, an app with no
# `placement` gets `nodeSelector: {kubernetes.io/hostname: DEFAULT_NODE}`, so all
# such pods land on that one node — handy for single-node testing (set it to your
# VM's name). An app's own `placement` always wins over this. Empty (default) =
# no fallback pin, unplaced pods schedule anywhere.
DEFAULT_NODE = os.environ.get("DEFAULT_NODE", "").strip()

# The well-known node label every node carries (its name). We pin by hostname
# rather than a custom `tier=` label so it works on any cluster with no extra
# node labelling.
HOSTNAME_LABEL = "kubernetes.io/hostname"

# When an app's pods are split across nodes (placement.nodes), the controller
# creates ONE Deployment per node, all sharing the app's Service and ConfigMap.
# Two Deployments can't carry the same selector (they'd fight over each other's
# pods), so each per-node Deployment gets a unique INSTANCE_LABEL on its
# selector + pods. The Service still selects {template, role} — a superset — so
# it fronts every pod regardless of which sub-Deployment owns it.
INSTANCE_LABEL = "app.kubernetes.io/instance"

# Must match IPERF_BASE_PORT in worker/state.py. The controller uses this
# when assigning explicit ip:port peer entries so source pods land on a
# port that is actually running in the target's iperf3 server pool.
IPERF_BASE_PORT = 9999

# Every resource we create is labelled so we can find/list/delete them
# later by label selector instead of by tracking names in memory.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "emulator-controller"
TEMPLATE_LABEL = "template"
ROLE_LABEL = "role"

# Each managed ConfigMap carries the original template JSON as an
# annotation so GET /templates/<name> can reconstruct the template from
# the cluster without the controller maintaining in-memory state.
TEMPLATE_ANNOTATION = "emulator.local/template"

# Which ingestion path created the template: "http" (POST /templates) or
# "watch" (declarative ConfigMap labelled with TEMPLATE_LABEL_FILTER, see
# watcher.py). The ConfigMap-watch reconciler only tears down templates
# whose source is "watch", so an HTTP-created template is never killed
# by a missing labelled ConfigMap.
SOURCE_ANNOTATION = "emulator.local/source"
SOURCE_HTTP = "http"
SOURCE_WATCH = "watch"


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

def _effective_count(app: dict) -> int:
    """Total pod count for an app.

    Explicit top-level `count` takes precedence. When omitted (only valid
    with the `placements` array form), the total is derived from the entries:
    each `site` entry contributes its count; each `sites` entry contributes
    count × len(sites)."""
    if "count" in app:
        return int(app["count"])
    total = 0
    for entry in app.get("placements") or []:
        c = int(entry.get("count", 0))
        if entry.get("site"):
            total += c
        else:
            total += c * len(entry.get("sites") or [])
    return total


def _validate_node_site_mapping(template: dict) -> dict[str, str] | None:
    """Validate template.node_site_mapping and return {site_name: k8s_node}.

    Each entry is {"k8s_node": <non-empty str>, "site_name": <non-empty str>}.
    This is the template's own registry of physical nodes, replacing node
    labels entirely: `placements` and `network_links` reference sites by
    name, and this mapping resolves each name to a real k8s node the
    controller pins pods to directly (nodeSelector on kubernetes.io/hostname)
    — no `kubectl label` step required.

    site_name values must be unique (it's the lookup key); the same k8s_node
    may be reused across multiple site_names (e.g. testing several sites on
    one physical node/VM).

    Returns None when the template declares no mapping at all — apps whose
    placements reference a `site`/`sites` then fail validation (see
    _validate_placement), since there's nothing to resolve them against."""
    mapping = template.get("node_site_mapping")
    if mapping is None:
        return None
    if not isinstance(mapping, list) or not mapping:
        raise ValueError(
            "node_site_mapping must be a non-empty array of "
            '{"k8s_node", "site_name"} objects')
    result: dict[str, str] = {}
    for i, entry in enumerate(mapping):
        if not isinstance(entry, dict):
            raise ValueError(f"node_site_mapping[{i}] must be an object")
        node = entry.get("k8s_node")
        site = entry.get("site_name")
        if not isinstance(node, str) or not node:
            raise ValueError(
                f"node_site_mapping[{i}].k8s_node must be a non-empty string")
        if not isinstance(site, str) or not site:
            raise ValueError(
                f"node_site_mapping[{i}].site_name must be a non-empty string")
        if site in result:
            raise ValueError(
                f"node_site_mapping: site_name {site!r} is declared more "
                "than once")
        result[site] = node
    return result


def _validate_placement(app_name: str, app: dict,
                        site_to_node: dict[str, str] | None) -> None:
    """Validate an app's optional placement — where its pods actually run.

    Two mutually-exclusive forms are accepted:

    `placement` (object) — three modes, all existing behaviour preserved:
      {"on": {<label>: <value>, ...}, "spread": true?}
      {"node": "<hostname>"}
      {"nodes": [{"node": "<hostname>", "count": N}, ...]}

    `placements` (array) — site-based declarative form, resolved through the
    template's own `node_site_mapping` (see _validate_node_site_mapping) —
    no node labelling needed:

      [{"site": "site-A",              "count": 1},
       {"sites": ["site-B","site-C"], "count": 1}]

      - "site"  (string) — exactly `count` pods on that site's node.
      - "sites" (array)  — `count` pods at *each* listed site (one Deployment
        per site); the app's top-level count must equal count × len(sites).

    Every site named here must appear in the template's node_site_mapping —
    raises ValueError immediately (a clean 400) rather than leaving pods
    Pending with an unresolvable nodeSelector.

    The app's top-level `count` must equal the sum of all effective pod counts
    across all placements entries."""
    placement = app.get("placement")
    placements = app.get("placements")

    if placement is not None and placements is not None:
        raise ValueError(
            f"app {app_name!r}: use 'placement' (object) or 'placements' "
            "(array), not both")

    # ── placements (array) validation ─────────────────────────────────────
    if placements is not None:
        if not isinstance(placements, list) or not placements:
            raise ValueError(
                f"app {app_name!r}.placements must be a non-empty array of "
                "placement entries")
        total = 0
        for i, entry in enumerate(placements):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"app {app_name!r}.placements[{i}] must be an object")
            site = entry.get("site")
            sites = entry.get("sites")
            if site is None and sites is None:
                raise ValueError(
                    f"app {app_name!r}.placements[{i}]: must have 'site' "
                    "(string) or 'sites' (array)")
            if site is not None and sites is not None:
                raise ValueError(
                    f"app {app_name!r}.placements[{i}]: use 'site' or "
                    "'sites', not both")
            c = entry.get("count")
            if isinstance(c, bool) or not isinstance(c, int) or c < 1:
                raise ValueError(
                    f"app {app_name!r}.placements[{i}].count must be a "
                    "positive int")
            if site is not None:
                if not isinstance(site, str) or not site:
                    raise ValueError(
                        f"app {app_name!r}.placements[{i}].site must be a "
                        "non-empty string")
                names = [site]
                total += c
            else:
                if not isinstance(sites, list) or not sites:
                    raise ValueError(
                        f"app {app_name!r}.placements[{i}].sites must be a "
                        "non-empty array of site name strings")
                for j, s in enumerate(sites):
                    if not isinstance(s, str) or not s:
                        raise ValueError(
                            f"app {app_name!r}.placements[{i}].sites[{j}] "
                            "must be a non-empty string")
                names = sites
                total += c * len(sites)
            for nm in names:
                if site_to_node is None or nm not in site_to_node:
                    raise ValueError(
                        f"app {app_name!r}.placements[{i}]: site {nm!r} has "
                        "no matching entry in the template's "
                        "node_site_mapping")
        explicit = app.get("count")
        if explicit is not None and total != int(explicit):
            raise ValueError(
                f"app {app_name!r}.placements: effective pod count is {total} "
                f"but app.count is {explicit} — they must match "
                "('site' entries add their count; 'sites' entries add "
                "count × len(sites))")
        return

    # ── placement (object) validation ─────────────────────────────────────
    if placement is None:
        return
    if not isinstance(placement, dict):
        raise ValueError(
            f"app {app_name!r}.placement must be an object — one of "
            '{"on": {...}}, {"node": "..."}, or {"nodes": [...]}')
    modes = [k for k in ("on", "node", "nodes") if placement.get(k) is not None]
    if len(modes) > 1:
        raise ValueError(
            f"app {app_name!r}.placement: use only one of 'on', 'node', or "
            f"'nodes' (got {', '.join(modes)})")
    spread = placement.get("spread")
    if spread is not None and not isinstance(spread, bool):
        raise ValueError(f"app {app_name!r}.placement.spread must be true or false")
    if spread and "on" not in modes:
        raise ValueError(
            f"app {app_name!r}.placement.spread only applies with 'on'")

    on = placement.get("on")
    if on is not None:
        if not isinstance(on, dict) or not on:
            raise ValueError(
                f"app {app_name!r}.placement.on must be a non-empty object of "
                "node label name/value pairs")
        for k, v in on.items():
            if not isinstance(k, str) or not k or not isinstance(v, str):
                raise ValueError(
                    f"app {app_name!r}.placement.on must map label names to "
                    "string values")

    node = placement.get("node")
    if node is not None and (not isinstance(node, str) or not node):
        raise ValueError(
            f"app {app_name!r}.placement.node must be a non-empty string")

    nodes = placement.get("nodes")
    if nodes is not None:
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(
                f"app {app_name!r}.placement.nodes must be a non-empty list of "
                '{"node", "count"} objects')
        seen: set[str] = set()
        total = 0
        for i, entry in enumerate(nodes):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"app {app_name!r}.placement.nodes[{i}] must be an object")
            n = entry.get("node")
            if not isinstance(n, str) or not n:
                raise ValueError(
                    f"app {app_name!r}.placement.nodes[{i}].node must be a "
                    "non-empty string")
            if n in seen:
                raise ValueError(
                    f"app {app_name!r}.placement.nodes lists node {n!r} more "
                    "than once")
            seen.add(n)
            c = entry.get("count")
            if isinstance(c, bool) or not isinstance(c, int) or c < 1:
                raise ValueError(
                    f"app {app_name!r}.placement.nodes[{i}].count must be a "
                    "positive int")
            total += c
        if total != _effective_count(app):
            raise ValueError(
                f"app {app_name!r}.placement.nodes counts sum to {total}, but "
                f"the app's count is {_effective_count(app)} — they must match "
                "(count is the total number of pods)")


def validate(template: dict) -> None:
    """Raise ValueError on any structural problem in the template."""
    if not isinstance(template, dict):
        raise ValueError("template must be a JSON object")
    name = template.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("template.name must be a non-empty string")
    # k8s names: lowercase alphanumeric + '-', 1-63 chars after the
    # 'wt-' + '-' overhead we add per role.
    if not all(c.islower() or c.isdigit() or c == "-" for c in name):
        raise ValueError(f"template.name {name!r}: lowercase alphanumeric + '-' only")
    # `apps` are the workloads (each materialises into one k8s "role":
    # Deployment + Service + ConfigMap). The internal code below still calls
    # these worker groups "roles" because that is the k8s-level concept they
    # become (the `role` label, the wt-<name>-<role> resource names).
    roles = template.get("apps")
    if not isinstance(roles, dict) or not roles:
        raise ValueError("template.apps must be a non-empty object")
    # The template's own site → k8s node registry (see
    # _validate_node_site_mapping). None when the template declares none —
    # `placements` then has nothing to resolve against and 400s if used.
    site_to_node = _validate_node_site_mapping(template)
    for role_name, role in roles.items():
        if not isinstance(role, dict):
            raise ValueError(f"app {role_name!r} must be an object")
        count = role.get("count")
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError(f"app {role_name!r}.count must be a positive int")
        elif not isinstance(role.get("placements"), list):
            raise ValueError(
                f"app {role_name!r}: count is required unless placements is used")
        for axis in ("cpu", "ram", "net"):
            sub = role.get(axis)
            if not isinstance(sub, dict) or "a" not in sub or "b" not in sub:
                raise ValueError(f"app {role_name!r}.{axis} must have 'a' and 'b'")
            # The worker evaluates these as floats; a non-number here would
            # only fail inside the worker, after the template was accepted.
            for coef in ("a", "b"):
                v = sub[coef]
                if (isinstance(v, bool) or not isinstance(v, (int, float))
                        or not math.isfinite(v)):
                    raise ValueError(
                        f"app {role_name!r}.{axis}.{coef} must be a number, "
                        f"got {v!r}")
        # `placement` decides where this app's pods actually run — on nodes
        # matching arbitrary labels (`on` [+`spread`]), pinned to one `node`,
        # or split across `nodes` with per-node counts (one Deployment per
        # node). `placements` instead resolves site names through the
        # template's own node_site_mapping. See _validate_placement.
        _validate_placement(role_name, role, site_to_node)
    for edge in template.get("edges", []) or []:
        if edge.get("from") not in roles:
            raise ValueError(f"edge.from {edge.get('from')!r} not in apps")
        if edge.get("to") not in roles:
            raise ValueError(f"edge.to {edge.get('to')!r} not in apps")

    # Optional network links: per-site latency + bandwidth declarations.
    # Shape-checked here so a malformed entry 400s at POST time; applied to
    # the cluster via tc netem by netem.apply() at the end of materialise().
    # Cross-checked against node_site_mapping's site names when the template
    # declares one, so a typo'd site name 400s here instead of netem.py
    # silently skipping the link at materialise time.
    linkspec.validate_network_links(
        template.get("network_links"),
        valid_sites=set(site_to_node) if site_to_node is not None else None)

    # Optional time-varying scenario; validated here so a malformed one 400s
    # at POST/PATCH time instead of crashing the scenario runner later.
    scenario_x_timeline(template)

    # Cycle detection.  x is resolved per role by a topological pass over
    # the role graph (see _compute_resolved_x); a cycle would make the
    # system underdetermined, so reject it here so the caller gets a clean
    # 400 instead of a half-materialised template.
    _compute_resolved_x(template)


# ----------------------------------------------------------------------------
# Blueprint rendering
# ----------------------------------------------------------------------------

def _load_blueprint() -> str:
    with open(BLUEPRINT_PATH) as f:
        return f.read()


def _placement_targets(app: dict, app_name: str, template_name: str,
                       site_to_node: dict[str, str] | None) -> list[dict]:
    """Resolve an app's placement into one or more Deployment targets.

    Each target is {suffix, replicas, node_selector, spread} → one Deployment.

    placements (array) takes priority and expands to one Deployment per site.
    Each site name is resolved to a real k8s node via site_to_node (the
    template's own node_site_mapping — see _validate_node_site_mapping), and
    pinned with a hostname nodeSelector, same mechanism as placement.node.
    Each entry with "site" emits one target; each entry with "sites" emits
    one target *per site*.

    For the legacy placement (object) form, resolution order is:
      - placement.nodes  → one target per node (per-node Deployments)
      - placement.node   → one target pinned to that hostname
      - placement.on     → one target with a label nodeSelector (+spread)
      - DEFAULT_NODE      → one target pinned to that hostname (controller fallback)
      - none              → one target, scheduled anywhere
    Placement modes are mutually exclusive (enforced by _validate_placement)."""
    count = _effective_count(app)

    # ── placements (array) → one Deployment per site ──────────────────────
    placements_list = app.get("placements")
    if placements_list:
        # Single-node dev mode: DEFAULT_NODE collapses all placements into one
        # Deployment on that node, ignoring site resolution entirely.
        if DEFAULT_NODE:
            return [{
                "suffix": "",
                "replicas": count,
                "node_selector": {HOSTNAME_LABEL: DEFAULT_NODE},
                "spread": False,
            }]
        targets: list[dict] = []
        for entry in placements_list:
            site = entry.get("site")
            sites = entry.get("sites") or []
            entry_count = int(entry["count"])
            if site:
                targets.append({
                    "suffix": f"-{len(targets)}",
                    "replicas": entry_count,
                    "node_selector": {HOSTNAME_LABEL: site_to_node[site]},
                    "spread": False,
                })
            else:
                for s in sites:
                    targets.append({
                        "suffix": f"-{len(targets)}",
                        "replicas": entry_count,
                        "node_selector": {HOSTNAME_LABEL: site_to_node[s]},
                        "spread": False,
                    })
        return targets

    # ── placement (object) → existing logic ───────────────────────────────
    placement = app.get("placement") if isinstance(app.get("placement"), dict) else {}

    nodes = placement.get("nodes")
    if nodes:
        return [{
            "suffix": f"-{i}",
            "replicas": int(entry["count"]),
            "node_selector": {HOSTNAME_LABEL: entry["node"]},
            "spread": False,
        } for i, entry in enumerate(nodes)]

    if placement.get("node"):
        node_selector = {HOSTNAME_LABEL: placement["node"]}
    elif placement.get("on"):
        node_selector = dict(placement["on"])
    elif DEFAULT_NODE:
        node_selector = {HOSTNAME_LABEL: DEFAULT_NODE}
    else:
        node_selector = {}
    return [{
        "suffix": "",
        "replicas": count,
        "node_selector": node_selector,
        "spread": bool(placement.get("spread")),
    }]


def _render_app(blueprint: str, template_name: str, app_name: str, image: str,
                targets: list[dict]) -> list[dict]:
    """Render an app's k8s resources from the blueprint: ONE ConfigMap + ONE
    Service, plus one Deployment per placement target.

    For a single target the Deployment keeps its plain name `wt-<t>-<app>` and
    the {template, role} selector (unchanged behaviour). For multiple targets
    (placement.nodes) each Deployment gets a `-<i>` suffix and a unique
    INSTANCE_LABEL on its selector/pods so they don't fight over each other's
    pods; the shared Service still selects {template, role} and fronts them all.
    Each target's `node_selector` (and optional topology spread) is applied to
    that Deployment's pod spec."""
    rendered = (blueprint
                .replace("__TEMPLATE__", template_name)
                .replace("__ROLE__", app_name)
                .replace("__COUNT__", "1")       # per-target replicas set below
                .replace("__IMAGE__", image))
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")
    svc = next(d for d in docs if d.get("kind") == "Service")
    dep_blueprint = next(d for d in docs if d.get("kind") == "Deployment")

    out: list[dict] = [cm, svc]
    multi = len(targets) > 1
    for t in targets:
        dep = copy.deepcopy(dep_blueprint)
        if multi:
            inst = f"wt-{template_name}-{app_name}{t['suffix']}"
            dep["metadata"]["name"] = inst
            dep["spec"]["selector"]["matchLabels"][INSTANCE_LABEL] = inst
            dep["spec"]["template"]["metadata"]["labels"][INSTANCE_LABEL] = inst
        dep["spec"]["replicas"] = t["replicas"]
        pod_spec = dep["spec"]["template"]["spec"]
        if t["node_selector"]:
            pod_spec.setdefault("nodeSelector", {}).update(t["node_selector"])
        if t["spread"]:
            pod_spec["topologySpreadConstraints"] = [{
                "maxSkew": 1,
                "topologyKey": HOSTNAME_LABEL,
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": {TEMPLATE_LABEL: template_name,
                                                  ROLE_LABEL: app_name}},
            }]
        out.append(dep)
    return out


def compute_peers(template: dict) -> dict[str, list[str]]:
    """For each role, the list of Service DNS names of its outbound peers.

    This is the *intent* — what the template says — without resolving to
    pod IPs. Used by GET /templates/<name> and as the input to peer
    resolution at materialise time. The actual peer addresses written
    into worker ConfigMaps are produced by _resolve_peer_ips() below."""
    name = template["name"]
    roles = template["apps"]
    peers: dict[str, list[str]] = {role: [] for role in roles}
    for edge in template.get("edges", []) or []:
        src, dst = edge["from"], edge["to"]
        dst_service = f"wt-{name}-{dst}"
        # Self-edge with count == 1 means the pod targets its own
        # single-pod Service — would route back to the same pod. Allow
        # it (intra-role mesh with count > 1 is the legitimate case)
        # but log so the surprise is visible.
        if src == dst and _effective_count(roles[dst]) == 1:
            log.warning("template %s: self-edge on role %r with count=1 "
                        "will route back to the same pod", name, src)
        if dst_service not in peers[src]:
            peers[src].append(dst_service)
    return peers


def describe(template: dict) -> dict:
    """A summary of what materialising `template` produces — for the POST
    /template response.

    Surfaces every template section, plus the controller-DERIVED values you
    can't read off the template:

      - each app's `resolved_x` — the propagated cascade value its formulas
        evaluate at (source apps use the input x, downstream apps the summed
        upstream egress);
      - each app's resolved `deployments` — the (replicas, node_selector) of
        each Deployment the app materialises into, so a per-node split and the
        effective DEFAULT_NODE fallback are both visible;
      - the resolved `peers` per app.

    Call only on a validated template — it assumes the app graph has no cycle.
    """
    apps = template.get("apps", {}) or {}
    peers = compute_peers(template)
    resolved = _compute_resolved_x(template)
    name = template.get("name", "")
    site_to_node = _validate_node_site_mapping(template)

    def deployments(app_name, app):
        out = []
        for t in _placement_targets(app, app_name, name, site_to_node):
            dep = {"replicas": t["replicas"],
                   "node_selector": t["node_selector"] or None}
            if t["spread"]:
                dep["spread"] = True
            out.append(dep)
        return out

    return {
        "x": template.get("x", 0),
        "default_node": DEFAULT_NODE or None,
        "runtime_scenarios": len(template.get("runtime_scenarios") or []) or None,
        "node_site_mapping": site_to_node,
        "network_links": len(template.get("network_links") or []) or None,
        "apps": {
            app_name: {
                "count": _effective_count(app),
                "placement": app.get("placement"),
                "placements": app.get("placements"),
                "deployments": deployments(app_name, app),
                "resolved_x": round(float(resolved.get(app_name, 0.0)), 3),
            }
            for app_name, app in apps.items()
        },
        "peers": peers,
    }


def _get_endpoint_pods(service_name: str) -> list[tuple[str, str]]:
    """Return (pod_name, pod_ip) for each pod backing this Service.

    Uses the Endpoints `targetRef.name` field which k8s populates from
    the pod's own metadata.name — stable across re-lists."""
    ns = k8s.namespace()
    status, body = k8s.get(f"/api/v1/namespaces/{ns}/endpoints/{service_name}")
    if status != 200:
        return []
    data = json.loads(body)
    result: list[tuple[str, str]] = []
    for subset in data.get("subsets") or []:
        for addr in subset.get("addresses") or []:
            ip = addr.get("ip")
            ref = addr.get("targetRef") or {}
            pod_name = ref.get("name", "")
            if ip and pod_name:
                result.append((pod_name, ip))
            elif ip:
                result.append(("", ip))
    return result


def _owning_deployment(pod_name: str) -> str:
    """`<deployment>-<pod-template-hash>-<suffix>` → `<deployment>`."""
    return pod_name.rsplit("-", 2)[0]


def _rollout_complete(dep_name: str) -> bool:
    """True once every pod of Deployment `dep_name` runs its current spec and
    no pod from an older ReplicaSet is still active (the same test as
    `kubectl rollout status`). Terminating pods drop out of both these counts
    and the Service's Endpoints, so after this the Endpoints converge on
    exactly the Deployment's current pods."""
    status, body = k8s.get(_kind_path("Deployment", dep_name))
    if status != 200:
        return False
    dep = json.loads(body)
    want = dep.get("spec", {}).get("replicas", 1)
    st = dep.get("status") or {}
    return (st.get("observedGeneration", 0)
            >= dep.get("metadata", {}).get("generation", 0)
            and st.get("updatedReplicas", 0) == want
            and st.get("replicas", 0) == want
            and st.get("availableReplicas", 0) == want)


def _wait_for_endpoint_pods(service_name: str, want_count: int,
                             deployments: set[str] | None = None,
                             timeout: float = 30.0) -> list[tuple[str, str]]:
    """Poll until the Service's Ready (pod_name, ip) pairs are the app's.

    Without `deployments`: until at least `want_count` pairs are Ready.

    With `deployments` (the app's current Deployment names): only pods owned
    by those Deployments count, every one of them must have finished rolling
    out, and the count must be exactly `want_count`. Right after a
    re-materialise the Endpoints still list the previous pods — those of a
    just-pruned Deployment, or an older ReplicaSet mid rolling update — and
    they stay Ready until they terminate. Accepting them would hand upstream
    apps IPs that are about to disappear, and nothing re-resolves peers
    afterwards, so the edge would carry no traffic until the next
    materialise."""
    deadline = time.monotonic() + timeout
    last: list[tuple[str, str]] = []
    while time.monotonic() < deadline:
        last = _get_endpoint_pods(service_name)
        if deployments is None:
            if len(last) >= want_count:
                return last
        else:
            last = [(pod, ip) for (pod, ip) in last
                    if _owning_deployment(pod) in deployments]
            if (len(last) == want_count
                    and all(_rollout_complete(d) for d in deployments)):
                return last
        time.sleep(1.0)
    if last:
        log.warning("endpoints for %s: got %d of %d expected within %.0fs",
                    service_name, len(last), want_count, timeout)
    else:
        log.warning("endpoints for %s: none ready after %.0fs",
                    service_name, timeout)
    return last


def _resolve_peer_ips(
        template: dict,
        deployments_by_role: dict[str, set[str]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, int]], dict[str, int]]:
    """Expand each role's peer Services into bare pod IPs and assign each
    source pod a port offset so it lands on a unique iperf3 server port on
    every target it connects to.

    A source pod uses a single offset (IPERF_BASE_PORT + offset) on *all* of
    its target pods, so any two source pods that share a target must get
    distinct offsets — otherwise they collide on that target's single-session
    iperf3 server port. Offsets are assigned by greedy colouring: each source
    pod takes the smallest offset not already claimed by another pod it shares
    a target role with. This is correct for arbitrary fan-out/fan-in — a
    source feeding several shared sinks — unlike a per-first-target counter,
    which lets offsets overlap on a sink that isn't a source's first target.

    Returns
    -------
    peers_by_role : dict[role, list[ip]]
        Bare pod IPs the worker should connect to (no port — the port is
        derived from the pod's own assigned offset).
    port_offset_by_pod_by_role : dict[role, dict[pod_name, offset]]
        Each source pod's port offset (0-based). The worker connects to
        IPERF_BASE_PORT + offset on every peer IP.
    effective_server_count : dict[role, int]
        iperf3 server slots each target role needs: (highest offset of any
        pod connecting to it) + 1. May exceed the raw fanin since greedy
        colouring isn't guaranteed minimal, but it never collides.

    `deployments_by_role` names each app's current Deployments (as just
    applied by materialise). When given, only those Deployments' rolled-out
    pods are resolved — see _wait_for_endpoint_pods.
    """
    deps = deployments_by_role or {}
    name = template["name"]
    prefix = f"wt-{name}-"
    intended = compute_peers(template)
    resolved: dict[str, list[str]] = {role: [] for role in template["apps"]}
    offsets: dict[str, dict[str, int]] = {role: {} for role in template["apps"]}

    # Resolve each target Service → pod IPs once, and collect each source
    # role's pod list (sorted by name for deterministic, stable offsets).
    ip_cache: dict[str, list[str]] = {}
    src_pods_by_role: dict[str, list[tuple[str, str]]] = {}
    for src_role, services in intended.items():
        if not services:
            continue
        src_count = _effective_count(template["apps"][src_role])
        src_pods_by_role[src_role] = sorted(
            _wait_for_endpoint_pods(prefix + src_role, src_count,
                                    deps.get(src_role)),
            key=lambda t: t[0])
        for svc in services:
            if svc not in ip_cache:
                trole = svc[len(prefix):]
                want = _effective_count(template["apps"][trole])
                ip_cache[svc] = [ip for (_, ip)
                                 in _wait_for_endpoint_pods(svc, want,
                                                            deps.get(trole))]
            for ip in ip_cache[svc]:
                if ip not in resolved[src_role]:
                    resolved[src_role].append(ip)

    # Greedy colouring. used_by_target[role] is the set of offsets already
    # claimed by source pods connecting to that target role. A source pod
    # connects to *every* pod of each target role it points at, so all pods
    # of a target role share one offset namespace.
    used_by_target: dict[str, set[int]] = {r: set() for r in template["apps"]}
    for src_role in sorted(src_pods_by_role):
        target_roles = [svc[len(prefix):] for svc in intended[src_role]]
        for pod_name, _ in src_pods_by_role[src_role]:
            if not pod_name:
                continue
            offset = 0
            while any(offset in used_by_target[t] for t in target_roles):
                offset += 1
            offsets[src_role][pod_name] = offset
            for t in target_roles:
                used_by_target[t].add(offset)

    effective_server_count: dict[str, int] = {
        r: (max(used) + 1 if used else 0)
        for r, used in used_by_target.items()
    }

    return resolved, offsets, effective_server_count


def _compute_fanin(template: dict) -> dict[str, int]:
    """Number of source pods that will open inbound iperf3 connections to each role.

    The worker uses this to size its iperf3 server pool exactly, rather than
    relying on the static IPERF_PORT_COUNT env-var guess."""
    roles = template["apps"]
    fanin: dict[str, int] = {role: 0 for role in roles}
    for edge in template.get("edges", []) or []:
        src, dst = edge["from"], edge["to"]
        fanin[dst] += _effective_count(roles[src])
    return fanin


def _source_apps(template: dict) -> set[str]:
    """Apps with no inbound edge (self-edges ignored) — the graph's sources.

    These are the apps whose x is set directly by the template's `x`; every
    other app derives its x from upstream egress (see _compute_resolved_x)."""
    apps = set(template.get("apps") or {})
    targets = {e.get("to") for e in (template.get("edges") or [])
               if e.get("from") != e.get("to")}
    return apps - targets


def _normalise_x(x, template: dict) -> dict[str, float]:
    """Resolve the template's `x` field into {source_app: starting_x}.

    `x` may be either:
      - a number — every source app starts at that value (the classic form), or
      - an object {app: number} — each named source app starts at its own value;
        sources not listed default to 0. This is how one template drives several
        independent sub-systems (DAGs) from different starting points.

    Raises ValueError if `x` is the wrong shape, names an app not in the
    template, or names a non-source app (a downstream app derives its x from
    upstream egress, so it cannot be set directly)."""
    sources = _source_apps(template)
    apps = template.get("apps") or {}
    if x is None:
        return {s: 0.0 for s in sources}
    if isinstance(x, bool):
        raise ValueError("x must be a number or an object mapping source apps "
                         "to numbers")
    if isinstance(x, (int, float)):
        return {s: float(x) for s in sources}
    if isinstance(x, dict):
        seeds = {s: 0.0 for s in sources}
        for app, val in x.items():
            if app not in apps:
                raise ValueError(f"x: {app!r} is not an app in this template")
            if app not in sources:
                raise ValueError(
                    f"x: {app!r} has inbound edges, so it derives its x from "
                    "upstream and cannot be set directly; x may only be set "
                    "for source apps")
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"x[{app!r}] must be a number")
            seeds[app] = float(val)
        return seeds
    raise ValueError("x must be a number or an object mapping source apps to "
                     "numbers")


def _compute_resolved_x(template: dict) -> dict[str, float]:
    """For each app, the x value its load formulas should evaluate at.

    The template's `x` is treated as a *signal* that propagates through
    the app graph rather than a global constant every app shares:

      - A source app (no inbound edges) uses its starting x from the template's
        `x` field — a single number applies to every source, or an
        {app: number} map gives each source its own starting value (unlisted
        sources default to 0). See _normalise_x.
      - A downstream app's `x` is the sum of upstream app-total egress.
        For an upstream app U with count N and net coefficients (a, b),
        U contributes  N * max(0, a * x_U + b)  Mbps to each downstream's x.

    This lets a sink's CPU/RAM/NET formulas scale with the *actual* traffic
    arriving at it instead of the raw input, and lets one template run several
    independent sub-systems from different starting x values.

    Self-edges (intra-app mesh with count > 1) are legal traffic-wise but
    are skipped for x-resolution — an app can't feed its own x without
    making the system circular.

    Raises ValueError if `x` is malformed or the app graph contains a cycle.
    """
    roles = template["apps"]
    seeds = _normalise_x(template.get("x", 0), template)  # {source app: x}

    # Build dedup'd upstream / downstream adjacency over app pairs.
    upstreams: dict[str, set[str]] = {r: set() for r in roles}
    downstreams: dict[str, set[str]] = {r: set() for r in roles}
    for edge in template.get("edges", []) or []:
        src, dst = edge["from"], edge["to"]
        if src == dst:
            continue
        upstreams[dst].add(src)
        downstreams[src].add(dst)

    # Kahn's algorithm.  An app is ready to compute once every upstream
    # has produced its own x; at that point its x is the accumulated
    # sum of upstream app-total egress.
    resolved: dict[str, float] = {}
    accum: dict[str, float] = {r: 0.0 for r in roles}
    remaining: dict[str, int] = {r: len(upstreams[r]) for r in roles}

    queue: list[str] = []
    for r in roles:
        if remaining[r] == 0:
            resolved[r] = seeds.get(r, 0.0)
            queue.append(r)

    head = 0
    while head < len(queue):
        r = queue[head]
        head += 1
        role = roles[r]
        per_pod_egress = max(0.0,
                             float(role["net"]["a"]) * resolved[r]
                             + float(role["net"]["b"]))
        role_total_egress = _effective_count(role) * per_pod_egress
        for dn in downstreams[r]:
            accum[dn] += role_total_egress
            remaining[dn] -= 1
            if remaining[dn] == 0:
                resolved[dn] = accum[dn]
                queue.append(dn)

    if len(resolved) != len(roles):
        unresolved = sorted(r for r in roles if r not in resolved)
        raise ValueError(
            f"role graph has a cycle involving: {', '.join(unresolved)}"
        )
    return resolved


def _role_config(template: dict, role_name: str, peers: list[str],
                 resolved_x: float,
                 server_count: int | None = None,
                 port_offset_by_pod: dict[str, int] | None = None) -> dict:
    """The config.json payload that goes into a role's ConfigMap.

    resolved_x is the x value this role's formulas should evaluate at.
    It's the template's x for source roles and the sum of upstream
    role-total egress for downstream roles (see _compute_resolved_x).

    port_offset_by_pod maps each source pod's name to the iperf3 port
    offset it should use when connecting: actual_port = BASE + offset.
    Assigning unique offsets across source pods avoids the iperf3
    single-session limit — two pods never fight over the same server port.
    """
    role = template["apps"][role_name]
    cfg: dict = {
        "x": resolved_x,
        "cpu": role["cpu"],
        "ram": role["ram"],
        "net": role["net"],
        "peers": peers,
    }
    if server_count is not None:
        cfg["server_count"] = server_count
    if port_offset_by_pod:
        cfg["port_offset_by_pod"] = port_offset_by_pod
    return cfg


# ----------------------------------------------------------------------------
# Kubernetes API plumbing
# ----------------------------------------------------------------------------

def _kind_path(kind: str, name: str | None = None,
               label_selector: str | None = None) -> str:
    ns = k8s.namespace()
    if kind == "Deployment":
        base = f"/apis/apps/v1/namespaces/{ns}/deployments"
    elif kind == "ConfigMap":
        base = f"/api/v1/namespaces/{ns}/configmaps"
    elif kind == "Service":
        base = f"/api/v1/namespaces/{ns}/services"
    else:
        raise ValueError(f"unsupported kind {kind!r}")
    if name is not None:
        return f"{base}/{name}"
    if label_selector:
        from urllib.parse import quote
        return f"{base}?labelSelector={quote(label_selector)}"
    return base


def _apply(resource: dict) -> None:
    """Idempotent create-or-update for a single resource dict."""
    kind = resource["kind"]
    name = resource["metadata"]["name"]
    status, body = k8s.post(_kind_path(kind), resource)
    if status in (200, 201):
        log.info("created %s/%s", kind, name)
        return
    if status == 409:
        # Already exists: strategic merge patch with the new fields.
        status, body = k8s.patch(_kind_path(kind, name), resource)
        if 200 <= status < 300:
            log.info("patched %s/%s", kind, name)
            return
    log.error("apply %s/%s failed: status=%s body=%s",
              kind, name, status, body[:300])
    raise RuntimeError(f"k8s API error applying {kind}/{name}: {status}")


def _prune_orphan_deployments(template_name: str, app_name: str,
                              keep: set[str]) -> None:
    """Delete this app's Deployments that the template no longer declares.

    An app's Deployment names depend on how many placement targets it has:
    one target keeps the plain `wt-<t>-<app>`, several get a `-<i>` suffix
    each (see _render_app). So re-materialising a template whose placement
    changed target count doesn't just re-shape the existing Deployments —
    it *renames* them, and the ones under the old names keep running their
    pods. Those pods stay in the app's Service (it selects the broad
    {template, role}), so upstream apps keep sending them traffic and the
    emulated load no longer matches the template.

    Called after an app's Deployments are applied, so there is never a
    window with no Deployment for the app. Scoping the list by the `role`
    label rather than by name prefix keeps this exact: apps whose names
    share a prefix (`store`, `store-cache`) never prune each other.

    Never raises. A failed prune leaves stale pods running — bad, but not
    worse than aborting a materialise that has otherwise applied cleanly,
    and the next materialise retries the prune.
    """
    selector = (f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},"
                f"{TEMPLATE_LABEL}={template_name},{ROLE_LABEL}={app_name}")
    status, body = k8s.get(_kind_path("Deployment", label_selector=selector))
    if status != 200:
        log.warning("prune %s/%s: listing Deployments failed: status=%s",
                    template_name, app_name, status)
        return
    for item in json.loads(body).get("items", []):
        dep_name = item.get("metadata", {}).get("name")
        if not dep_name or dep_name in keep:
            continue
        # 404 means someone else already removed it — the desired end state.
        del_status, del_body = k8s.delete(_kind_path("Deployment", dep_name))
        if del_status in (200, 202, 404):
            log.info("pruned orphan Deployment/%s (template=%s role=%s): no "
                     "longer in the app's placement targets %s",
                     dep_name, template_name, app_name, sorted(keep))
        else:
            log.warning("prune Deployment/%s failed: status=%s body=%s — its "
                        "pods are still running and still in the %s Service",
                        dep_name, del_status, del_body[:200], app_name)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

def materialise(template: dict, source: str = SOURCE_HTTP) -> None:
    """Create (or update) all Kubernetes resources for the template.

    Two phases:
      1. Apply every Deployment/Service/ConfigMap with an empty peers
         list. Workers come up with their HTTP servers + iperf3 server
         pool but no outbound peer traffic. Each app is then pruned of
         Deployments the new placement no longer names — see
         _prune_orphan_deployments for why re-materialising can rename
         them.
      2. Wait for each Service's Endpoints to populate, then patch the
         per-role ConfigMap's `peers` field with the concrete pod IPs
         that are now backing each target Service. Workers re-read the
         file (via their watchdog) and spawn one iperf3 client per
         resolved peer IP.

    The two-phase approach avoids a startup race: if we wrote peer IPs
    in phase 1 they'd be empty (Endpoints haven't populated yet) and we'd
    have no way to recover without an external reconciler.

    `source` records which ingestion path triggered this call. Stamped
    into the SOURCE_ANNOTATION on every managed ConfigMap so the
    declarative watcher can tell HTTP-managed templates apart from its
    own and never tear those down.
    """
    validate(template)
    name = template["name"]
    log.info("Materialising template %s (source=%s)", name, source)
    blueprint = _load_blueprint()
    template_annotation = json.dumps(template, separators=(",", ":"))
    fanin = _compute_fanin(template)
    # x propagates through the role graph: source roles evaluate their
    # formulas at the template's x, downstream roles at the sum of
    # upstream role-total egress.  Computed once here and threaded
    # through both phases so Phase 1's initial config and Phase 2's
    # peer-IP patch agree.  validate() above already guarantees this
    # won't raise (cycles are rejected there).
    resolved_x = _compute_resolved_x(template)
    log.info("Template %s: resolved x per role = %s",
             name, {r: round(v, 3) for r, v in resolved_x.items()})
    site_to_node = _validate_node_site_mapping(template)

    # ─── Phase 1: create resources with empty peers ─────────────────────
    deployments_by_role: dict[str, set[str]] = {}
    for role_name, role in template["apps"].items():
        # `placement`/`placements` decides where the pods run: one Deployment
        # for the simple cases, or one per node/site when split across
        # several. All share the app's one Service + ConfigMap.
        targets = _placement_targets(role, role_name, name, site_to_node)
        docs = _render_app(blueprint, name, role_name, WORKER_IMAGE, targets)
        config_payload = json.dumps(
            _role_config(template, role_name, [],
                         resolved_x=resolved_x[role_name],
                         server_count=fanin[role_name] or None),
            indent=2,
        )
        for doc in docs:
            if doc.get("kind") == "ConfigMap":
                doc.setdefault("data", {})["config.json"] = config_payload
                meta = doc.setdefault("metadata", {})
                ann = meta.setdefault("annotations", {})
                ann[TEMPLATE_ANNOTATION] = template_annotation
                ann[SOURCE_ANNOTATION] = source
        applied_deployments: set[str] = set()
        for doc in docs:
            _apply(doc)
            if doc.get("kind") == "Deployment":
                applied_deployments.add(doc["metadata"]["name"])
        deployments_by_role[role_name] = applied_deployments
        # Re-materialising an app whose placement target count changed renames
        # its Deployments (`wt-<t>-<app>` ⇄ `wt-<t>-<app>-<i>`), so the ones
        # under the old names would otherwise keep running pods that the
        # template no longer declares — and keep serving them through the
        # app's Service. Drop them now that the new set is applied.
        _prune_orphan_deployments(name, role_name, applied_deployments)

    # ─── Phase 2: resolve peers to pod IPs and patch ConfigMaps ─────────
    log.info("Template %s: waiting for endpoints to populate…", name)
    peers_by_role, offsets_by_role, effective_sc = _resolve_peer_ips(
        template, deployments_by_role)
    for role_name in template["apps"]:
        peer_ips = peers_by_role[role_name]
        sc = effective_sc.get(role_name) or None

        # Patch roles that have outbound peers (the common case) OR roles
        # that are pure sinks whose effective server count differs from the
        # Phase 1 fanin estimate. The latter happens when a source role has
        # an inflated offset (because it shares a target with another source
        # role), causing it to connect to a higher-numbered port on the sink
        # than the sink's raw fanin would suggest — e.g. batch→cache AND
        # batch→storage where batch gets offsets 2,3 (not 0,1) because web
        # was processed first for cache, so storage also needs 4 servers
        # even though only 2 pods connect to it.
        needs_sc_update = sc is not None and sc != (fanin[role_name] or None)
        if not peer_ips and not needs_sc_update:
            continue

        cm_name = f"wt-{name}-{role_name}-config"
        config_payload = json.dumps(
            _role_config(template, role_name, peer_ips,
                         resolved_x=resolved_x[role_name],
                         server_count=sc,
                         port_offset_by_pod=offsets_by_role[role_name] or None),
            indent=2,
        )
        patch_body = {"data": {"config.json": config_payload}}
        status, body = k8s.patch(_kind_path("ConfigMap", cm_name), patch_body)
        if 200 <= status < 300:
            log.info("Template %s: wrote %d peer IPs + offsets %s (server_count=%s) into %s",
                     name, len(peer_ips), offsets_by_role[role_name], sc, cm_name)
        else:
            log.warning("Template %s: peer-IP patch on %s failed: %s",
                        name, cm_name, status)

    # Apply inter-site link shaping (latency + bandwidth) via tc netem on each
    # node's NIC.  Runs after pods are up so the shaping is in effect before
    # workers start exchanging traffic.  Never raises — shaping is auxiliary.
    netem.apply(template)


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge `patch` into a copy of `base`.

    Dict values merge recursively; scalars and lists in `patch` replace
    whatever's in `base`.  Neither input is mutated.  This is the merge
    semantics used by PATCH /templates/<name>: a partial template like
    `{"apps": {"middle": {"net": {"a": 0.3}}}}` updates just middle's
    net.a while leaving net.b and every other field untouched.
    """
    result = dict(base)
    for k, v in patch.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _expand_dot_keys(obj):
    """Expand dot-path keys in a patch into nested dicts, recursively.

    Lets callers write a flat shorthand instead of deeply nested JSON, e.g.
        {"x": 80, "apps.ingest.cpu.a": 5, "apps.ingest.cpu.b": 100}
    expands to
        {"x": 80, "apps": {"ingest": {"cpu": {"a": 5, "b": 100}}}}
    which then goes through the normal _deep_merge.  App/field names never
    contain dots, so a dot in a key is unambiguously a path separator.
    Non-dict values and dot-free
    keys pass through unchanged, so existing nested patches still work."""
    if not isinstance(obj, dict):
        return obj
    result: dict = {}
    for key, value in obj.items():
        value = _expand_dot_keys(value)
        parts = key.split(".") if isinstance(key, str) else [key]
        cursor = result
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        leaf = parts[-1]
        if isinstance(cursor.get(leaf), dict) and isinstance(value, dict):
            cursor[leaf] = _deep_merge(cursor[leaf], value)
        else:
            cursor[leaf] = value
    return result


def patch_template(name: str, patch: dict,
                   normalise: Callable[[dict], dict] | None = None,
                   ) -> dict | None:
    """Merge `patch` into the existing template `name` and re-materialise.

    The merge is deep — see `_deep_merge` — so a patch only needs to
    specify the fields that actually change.  Patch keys may also use
    dot-path shorthand (see `_expand_dot_keys`): `{"apps.ingest.cpu.a": 5}`
    is equivalent to the fully-nested form.  The template's `name`
    field is always taken from the URL parameter (any `name` in the
    patch body is ignored).  The source annotation (http vs watch) is
    preserved so a watch-managed template stays under watcher control
    after a PATCH; the next labelled-ConfigMap reconciliation will still
    overwrite it from the labelled-CM content, so callers PATCHing a
    watch-managed template should usually also update the labelled CM.

    Returns the merged template on success, or None if no template
    named `name` exists.  Raises ValueError if the merged template
    fails validation (including cycle detection), and RuntimeError if a
    Kubernetes API call fails during re-materialisation.  materialise()
    is idempotent, so a partial failure leaves the cluster in a
    well-defined state that a retry can recover.

    `normalise`, when given, is applied to the merged template before
    validation and its result is what gets materialised — the HTTP layer
    passes its POST schema here so a PATCH is checked (and stored) exactly
    like a POST of the merged template. It raises ValueError to reject.
    """
    info = get_managed(name)
    if info is None or not info.get("template"):
        return None
    existing = info["template"]
    source = info.get("source") or SOURCE_HTTP
    # Expand dot-path shorthand, then strip name (the URL is the source of
    # truth), then deep-merge onto the existing template.
    expanded = _expand_dot_keys(patch)
    sanitized = {k: v for k, v in expanded.items() if k != "name"}
    merged = _deep_merge(existing, sanitized)
    merged["name"] = name
    if normalise is not None:
        merged = normalise(merged)
    validate(merged)
    log.info("Patching template %s (source=%s) with: %s", name, source,
             json.dumps(sanitized, separators=(",", ":")))
    materialise(merged, source=source)
    return merged


# ----------------------------------------------------------------------------
# Runtime scenarios (scenario runner support, see runner.py)
# ----------------------------------------------------------------------------

def scenario_x_timeline(template: dict) -> list[dict]:
    """Normalise a template's optional `runtime_scenarios` into the ordered list
    of phases the scenario runner steps through.

    Each scenario entry is
        {"phase_id"?, "start_min", "end_min", "x"}
    a wall-clock window (minutes since the runner started) during which the
    system input x holds at `x`. Returns [] when the template declares none.

    Raises ValueError on a malformed scenario so it 400s at POST/PATCH time
    rather than crashing the runner thread. Each phase's `x` may be a single
    number (every source app) or an {app: number} map (per-source starting
    values); it is validated against the template's source apps here — see
    _normalise_x — and applied verbatim by patch_system_x.
    """
    raw = template.get("runtime_scenarios")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("runtime_scenarios must be a list of phase objects")
    phases: list[dict] = []
    for i, ph in enumerate(raw):
        if not isinstance(ph, dict):
            raise ValueError(f"runtime_scenarios[{i}] must be an object")
        try:
            start = float(ph["start_min"])
            end = float(ph["end_min"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"runtime_scenarios[{i}] needs numeric start_min and end_min")
        if "x" not in ph:
            raise ValueError(f"runtime_scenarios[{i}] needs an x")
        # x may be a scalar or a {source_app: value} map; validate its shape
        # against the template's sources now so a bad phase 400s at POST time.
        try:
            _normalise_x(ph["x"], template)
        except ValueError as exc:
            raise ValueError(f"runtime_scenarios[{i}].x: {exc}")
        if end <= start:
            raise ValueError(
                f"runtime_scenarios[{i}]: end_min must be greater than start_min")
        phases.append({
            "phase_id": str(ph.get("phase_id") or f"phase-{i}"),
            "start_min": start,
            "end_min": end,
            "x": ph["x"],  # scalar or map, applied verbatim by patch_system_x
        })
    phases.sort(key=lambda p: p["start_min"])
    return phases


def patch_system_x(name: str, x) -> dict | None:
    """Replace the materialised template's `x` with `x` and re-materialise.

    The scenario runner calls this to step the system input over time. `x` is a
    scalar (every source app) or a {source_app: value} map; it **replaces** the
    existing `x` wholesale — a per-phase map fully defines that phase's starting
    points (unlisted sources fall to 0), so values from a previous phase can't
    linger (unlike the merging PATCH /template path). Every app's ConfigMap
    picks up the newly resolved x and workers hot-reload via their file watcher —
    no pod restart. Returns the merged template, or None if `name` isn't
    materialised.
    """
    info = get_managed(name)
    if info is None or not info.get("template"):
        return None
    merged = dict(info["template"])
    merged["x"] = x
    merged["name"] = name
    source = info.get("source") or SOURCE_HTTP
    validate(merged)
    materialise(merged, source=source)
    return merged


def teardown(name: str) -> int:
    """Delete every resource we created for `name`. Returns count deleted."""
    log.info("Tearing down template %s", name)
    # Fetch the template BEFORE deleting anything: node_site_mapping lives in
    # the ConfigMap annotation we're about to delete, and netem.teardown()
    # below needs it to know which real nodes to clear tc rules from.
    info = get_managed(name)
    node_site_mapping = ((info or {}).get("template") or {}).get("node_site_mapping")
    selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{TEMPLATE_LABEL}={name}"
    deleted = 0
    # Deployments first so pods stop using the ConfigMap before it goes.
    for kind in ("Deployment", "Service", "ConfigMap"):
        path = _kind_path(kind, label_selector=selector)
        status, body = k8s.delete(path)
        if status in (200, 202):
            try:
                obj = json.loads(body)
                if obj.get("kind", "").endswith("List"):
                    deleted += len(obj.get("items", []))
                else:
                    deleted += 1
            except json.JSONDecodeError:
                deleted += 1
        elif status == 404:
            continue
        else:
            log.warning("delete %s for template %s: status=%s body=%s",
                        kind, name, status, body[:200])
    netem.teardown(node_site_mapping)
    return deleted


def list_managed() -> list[str]:
    """Names of templates currently materialised in the cluster."""
    selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
    status, body = k8s.get(_kind_path("ConfigMap", label_selector=selector))
    if status != 200:
        return []
    names: set[str] = set()
    for item in json.loads(body).get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        if TEMPLATE_LABEL in labels:
            names.add(labels[TEMPLATE_LABEL])
    return sorted(names)


def get_managed(name: str) -> dict | None:
    """Reconstruct a materialised template's full state from the cluster.

    Returns None if no resources are labelled with the given template
    name. Otherwise returns a dict combining:
      - the original template (read from the annotation we stamped at
        materialise time),
      - the resolved peers per role (recomputed from the template's
        edges so it stays in sync if the annotation lags),
      - the current replica counts as reported by each role's Deployment
        (so a `kubectl scale` is visible here),
      - the names of the resources we created.
    """
    selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{TEMPLATE_LABEL}={name}"

    cm_status, cm_body = k8s.get(_kind_path("ConfigMap", label_selector=selector))
    if cm_status != 200:
        return None
    cms = json.loads(cm_body).get("items", [])
    if not cms:
        return None

    # All ConfigMaps for one template carry the same annotations.
    template_json = None
    source = None
    for cm in cms:
        ann = cm.get("metadata", {}).get("annotations", {}) or {}
        template_json = template_json or ann.get(TEMPLATE_ANNOTATION)
        source = source or ann.get(SOURCE_ANNOTATION)
        if template_json and source:
            break
    template: dict | None = None
    if template_json:
        try:
            template = json.loads(template_json)
        except json.JSONDecodeError:
            log.warning("template %s: annotation is not valid JSON", name)

    dep_status, dep_body = k8s.get(_kind_path("Deployment", label_selector=selector))
    replicas: dict[str, int] = {}
    if dep_status == 200:
        for dep in json.loads(dep_body).get("items", []):
            labels = dep.get("metadata", {}).get("labels", {})
            role = labels.get(ROLE_LABEL)
            if role:
                # An app may have several Deployments (one per node when
                # placement.nodes splits its replicas) — sum them for the total.
                replicas[role] = (replicas.get(role, 0)
                                  + int(dep.get("spec", {}).get("replicas", 0)))

    return {
        "name": name,
        "source": source,
        "template": template,
        "peers": compute_peers(template) if template else {},
        "replicas": replicas,
        "configmaps": sorted(cm.get("metadata", {}).get("name") for cm in cms),
    }


def list_managed_with_source(source: str) -> dict[str, dict]:
    """Map of template name → parsed template, for templates created by
    the given ingestion path. Used by the watcher to find templates it
    owns so it can compare against the labelled-CM expected set."""
    selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
    status, body = k8s.get(_kind_path("ConfigMap", label_selector=selector))
    if status != 200:
        return {}
    result: dict[str, dict] = {}
    for cm in json.loads(body).get("items", []):
        labels = cm.get("metadata", {}).get("labels", {}) or {}
        ann = cm.get("metadata", {}).get("annotations", {}) or {}
        if ann.get(SOURCE_ANNOTATION) != source:
            continue
        name = labels.get(TEMPLATE_LABEL)
        tj = ann.get(TEMPLATE_ANNOTATION)
        if not name or not tj or name in result:
            continue
        try:
            result[name] = json.loads(tj)
        except json.JSONDecodeError:
            log.warning("template %s: annotation is not valid JSON", name)
    return result
