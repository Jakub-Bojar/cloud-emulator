"""
In-memory stand-in for the Kubernetes API, for controller tests.

The controller talks to the API through exactly one chokepoint —
`k8s.request(method, path, body)` — so a test can swap the whole cluster
out by patching that single function. FakeCluster implements enough of
the REST surface the materialiser uses (namespaced Deployments, Services
and ConfigMaps; create / strategic-merge-patch / get / list-by-label /
delete-by-name / delete-by-label) to exercise materialise() end to end
without a cluster.

Deliberate simplifications, none of which the materialiser depends on:
  - PATCH deep-merges dicts and replaces lists, so it does not honour the
    patchMergeKey semantics real strategic-merge uses for lists such as
    `containers`. The materialiser only relies on scalar and map fields
    (`spec.replicas`, `spec.template.spec.nodeSelector`) surviving a patch.
    - Nodes are a fixed list of names (`nodes`), served at /api/v1/nodes.
  - No resourceVersion, no admission, no defaulting, no controllers — a
    Deployment never produces Pods or status on its own. A test plays the
    controllers' part with set_endpoints() and roll_out(); a Service with
    no Endpoints set 404s, as it would before its first Ready pod. The one
    thing kept is `metadata.generation`, bumped on every Deployment spec
    change, so a roll_out() from before a patch reads as stale — the
    signal real rollout checks key on.
"""

import copy
import json
import urllib.parse

NAMESPACE = "emulator"

# What netem.apply() reports for a template without network_links; tests that
# stub shaping out return this.
NO_LINKS = {"status": "none", "links": {}}

# API path segment → the `kind` its list response reports.
_LIST_KIND = {
    "deployments": "DeploymentList",
    "services": "ServiceList",
    "configmaps": "ConfigMapList",
    "endpoints": "EndpointsList",
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _matches(obj: dict, selector: str) -> bool:
    labels = obj.get("metadata", {}).get("labels", {}) or {}
    for term in selector.split(","):
        if not term:
            continue
        key, _, value = term.partition("=")
        if labels.get(key) != value:
            return False
    return True


class FakeCluster:
    """A namespace's worth of objects, keyed by collection then name."""

    def __init__(self):
        self.objects: dict[str, dict[str, dict]] = {
            "deployments": {}, "services": {}, "configmaps": {},
            "endpoints": {}}
        # Cluster-scoped: the nodes GET /api/v1/nodes lists. Defaults to the
        # three-node cluster the tests' templates name; None makes it 403.
        self.nodes: list[str] | None = ["microk8s-vm", "site-b", "site-c"]
        # Every (method, collection, name) tuple the code under test issued,
        # so a test can assert on what was *not* touched as well as what was.
        self.calls: list[tuple[str, str, str | None]] = []

    # ── helpers for assertions ────────────────────────────────────────────

    def names(self, collection: str) -> set[str]:
        return set(self.objects[collection])

    def deployment(self, name: str) -> dict:
        return self.objects["deployments"][name]

    def replicas(self, name: str) -> int:
        return self.deployment(name)["spec"]["replicas"]

    def node_selector(self, name: str) -> dict:
        pod_spec = self.deployment(name)["spec"]["template"]["spec"]
        return pod_spec.get("nodeSelector", {})

    def deleted(self, collection: str) -> list[str]:
        return [name for method, coll, name in self.calls
                if method == "DELETE" and coll == collection and name]

    # ── playing the controllers' part ─────────────────────────────────────

    def set_endpoints(self, service: str, pods: list[tuple[str, str]]) -> None:
        """Make `pods` [(pod_name, ip)] the Service's Ready addresses."""
        self.objects["endpoints"][service] = {
            "metadata": {"name": service},
            "subsets": [{"addresses": [
                {"ip": ip, "targetRef": {"kind": "Pod", "name": pod}}
                for pod, ip in pods]}] if pods else [],
        }

    def roll_out(self, name: str) -> None:
        """Report Deployment `name`'s current spec as fully rolled out."""
        dep = self.deployment(name)
        n = dep["spec"]["replicas"]
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"],
                         "replicas": n, "updatedReplicas": n,
                         "availableReplicas": n}

    # ── the k8s.request replacement ───────────────────────────────────────

    def request(self, method: str, path: str, body: bytes | None = None,
                content_type: str = "application/json",
                timeout: float = 10.0) -> tuple[int, bytes]:
        parsed = urllib.parse.urlparse(path)
        segments = parsed.path.strip("/").split("/")
        if segments == ["api", "v1", "nodes"] and method == "GET":
            self.calls.append((method, "nodes", None))
            if self.nodes is None:      # e.g. RBAC lost the ClusterRole
                return 403, self._status("nodes is forbidden")
            return 200, json.dumps({"kind": "NodeList", "items": [
                {"metadata": {"name": n}} for n in self.nodes]}).encode()
        # Every other path the materialiser builds is namespaced:
        #   .../namespaces/<ns>/<collection>[/<name>]
        idx = segments.index("namespaces")
        collection = segments[idx + 2]
        name = segments[idx + 3] if len(segments) > idx + 3 else None
        selector = urllib.parse.parse_qs(parsed.query).get("labelSelector", [""])[0]
        self.calls.append((method, collection, name))

        if collection not in self.objects:
            return 404, self._status(f"no such collection {collection!r}")

        store = self.objects[collection]
        payload = json.loads(body) if body else None

        if method == "GET":
            if name is not None:
                if name not in store:
                    return 404, self._status(f"{name} not found")
                return 200, json.dumps(store[name]).encode()
            items = [o for o in store.values()
                     if not selector or _matches(o, selector)]
            return 200, json.dumps(
                {"kind": _LIST_KIND[collection], "items": items}).encode()

        if method == "POST":
            new_name = payload["metadata"]["name"]
            if new_name in store:
                return 409, self._status(f"{new_name} already exists")
            store[new_name] = copy.deepcopy(payload)
            if collection == "deployments":
                store[new_name]["metadata"]["generation"] = 1
            return 201, json.dumps(store[new_name]).encode()

        if method == "PATCH":
            if name not in store:
                return 404, self._status(f"{name} not found")
            before = store[name]
            store[name] = _deep_merge(before, copy.deepcopy(payload))
            if (collection == "deployments"
                    and store[name].get("spec") != before.get("spec")):
                store[name]["metadata"]["generation"] = (
                    before["metadata"].get("generation", 1) + 1)
            return 200, json.dumps(store[name]).encode()

        if method == "DELETE":
            if name is not None:
                if name not in store:
                    return 404, self._status(f"{name} not found")
                return 200, json.dumps(store.pop(name)).encode()
            removed = [n for n, o in store.items() if _matches(o, selector)]
            for n in removed:
                del store[n]
            return 200, json.dumps(
                {"kind": _LIST_KIND[collection],
                 "items": [{"metadata": {"name": n}} for n in removed]}).encode()

        return 405, self._status(f"unsupported method {method}")

    @staticmethod
    def _status(message: str) -> bytes:
        return json.dumps({"kind": "Status", "message": message}).encode()
