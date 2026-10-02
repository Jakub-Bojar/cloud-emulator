"""
Regression tests for the controller saying "yes" when it shouldn't.

Found on the live cluster:
  - A template naming a node the cluster doesn't have (all shipped templates
    use `site-a`, for the RUNBOOK's 5-node lab) was accepted with a 201 after
    a 31 s stall, its pods Pending forever.
  - With the controller in-cluster, `network_links` were never shaped (no
    `multipass` in the pod), yet PATCH returned 200 and /metrics advertised
    the configured latency — only the controller log said otherwise.

Now a template naming a missing node is a 400; POST/PATCH responses carry
`warnings` and `network_shaping`; /overview and
emulator_link_shaping_applied{pair} say whether each link is in effect.

Run from the repo root:

    python -m unittest discover -s tests
"""

import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "controller"))

from fastapi.testclient import TestClient  # noqa: E402

import app as controller_app  # noqa: E402
import k8s  # noqa: E402
import materialiser  # noqa: E402
import netem  # noqa: E402

from fake_k8s import NAMESPACE, NO_LINKS, FakeCluster  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BLUEPRINT = REPO_ROOT / "manifests" / "worker-template.yaml"

AXES = {"cpu": {"a": 5, "b": 50}, "ram": {"a": 1, "b": 64},
        "net": {"a": 0.1, "b": 1}}
# An end-of-graph app sends nothing, so it declares no network load (one that
# does gets a warning — see SinkNetWarningTest).
SINK = {**AXES, "net": {"a": 0, "b": 0}}
MAPPING = [{"k8s_node": "microk8s-vm", "site_name": "site-A"},
           {"k8s_node": "site-b", "site_name": "site-B"},
           {"k8s_node": "site-c", "site_name": "site-C"}]
# What netem._resolve_sites would find for MAPPING on the live cluster.
SITES = {"site-A": ("microk8s-vm", "192.168.2.2"),
         "site-B": ("site-b", "192.168.2.89"),
         "site-C": ("site-c", "192.168.2.90")}
B_C = "site-B ↔ site-C"
A_B = "site-A ↔ site-B"


def template(mapping=MAPPING, links=None, edges=None, **apps) -> dict:
    t = {"name": "report-test", "x": 10, "node_site_mapping": mapping,
         "apps": apps or {"store": {**SINK, "placements": [
             {"site": "site-B", "count": 1}]}}}
    if links is not None:
        t["network_links"] = links
    if edges is not None:
        t["edges"] = edges
    return t


class ControllerTestCase(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        for patcher in (
                mock.patch.object(k8s, "request", self.cluster.request),
                mock.patch.object(k8s, "namespace", lambda: NAMESPACE),
                mock.patch.object(materialiser, "BLUEPRINT_PATH", str(BLUEPRINT)),
                mock.patch.object(materialiser, "DEFAULT_NODE", "")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)


# ── nodes the cluster doesn't have ────────────────────────────────────────────

class LiveNodeCheckTest(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self._patch(mock.patch.object(netem, "apply", lambda t: NO_LINKS))

    def test_unknown_mapped_node_is_rejected_before_anything_is_created(self):
        mapping = MAPPING + [{"k8s_node": "site-d", "site_name": "site-D"}]
        with self.assertRaisesRegex(ValueError, r"'site-d' \(node_site_mapping"
                                    r"\[3\] \(site 'site-D'\)\).*nodes are: "
                                    r"microk8s-vm, site-b, site-c"):
            materialiser.materialise(template(mapping))
        self.assertEqual(self.cluster.names("deployments"), set())
        self.assertEqual(self.cluster.names("configmaps"), set())

    def test_unknown_placement_node_is_rejected(self):
        app = {**AXES, "count": 1, "placement": {"node": "site-a"}}
        with self.assertRaisesRegex(ValueError, r"'site-a' \(app 'edge'"
                                    r"\.placement\.node\)"):
            materialiser.materialise(template(mapping=None, edge=app))

    def test_unknown_default_node_is_rejected(self):
        with mock.patch.object(materialiser, "DEFAULT_NODE", "microk8s"):
            with self.assertRaisesRegex(ValueError, "DEFAULT_NODE"):
                materialiser.materialise(template())

    def test_unlisted_nodes_warn_but_do_not_block(self):
        self.cluster.nodes = None   # 403
        report = materialiser.materialise(template())
        self.assertEqual(self.cluster.names("deployments"),
                         {"wt-report-test-store"})
        self.assertIn("node names were not checked", report["warnings"][0])

    def test_known_nodes_pass_without_warnings(self):
        report = materialiser.materialise(template())
        self.assertEqual(report["warnings"], [])

    def test_post_returns_400_naming_the_node(self):
        client = TestClient(controller_app.app)
        mapping = [{"k8s_node": "site-a", "site_name": "site-A"}]
        body = template(mapping, store={**AXES, "placements": [
            {"site": "site-A", "count": 1}]})
        r = client.post("/template", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("'site-a'", r.json()["error"])


# ── pods that never become Ready ──────────────────────────────────────────────

class EndpointTimeoutWarningTest(ControllerTestCase):
    def test_timeout_is_reported_not_just_logged(self):
        self._patch(mock.patch.object(netem, "apply", lambda t: NO_LINKS))
        clock = types.SimpleNamespace(now=0.0)

        def sleep(s):
            clock.now += s
        self._patch(mock.patch.object(
            materialiser, "time",
            types.SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)))
        # gateway → store, and no pod ever becomes Ready (no Endpoints).
        body = template(edges=[{"from": "gateway", "to": "store"}],
                        gateway={**AXES, "placements": [
                            {"site": "site-B", "count": 1}]},
                        store={**SINK, "placements": [
                            {"site": "site-C", "count": 2}]})
        report = materialiser.materialise(body)
        self.assertEqual(len(report["warnings"]), 2, report["warnings"])
        self.assertIn("wt-report-test-gateway: 0 of 1 pods Ready",
                      report["warnings"][0])
        self.assertIn("wt-report-test-store: 0 of 2 pods Ready",
                      report["warnings"][1])


# ── a declared network load with nowhere to go ─────────────────────────────

class SinkNetWarningTest(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self._patch(mock.patch.object(netem, "apply", lambda t: NO_LINKS))

    def test_sink_with_a_net_formula_is_warned_about(self):
        report = materialiser.materialise(template(store={
            **AXES, "placements": [{"site": "site-B", "count": 1}]}))
        self.assertEqual(report["warnings"], [
            "app 'store' has no outbound edges, so its net formula (2 Mbps "
            "per pod at x=10) generates no traffic; its net target reads 0"])

    def test_sink_without_one_and_senders_are_not(self):
        self.cluster.set_endpoints("wt-report-test-gateway",
                                   [("wt-report-test-gateway-abc-1", "10.0.0.1")])
        self.cluster.set_endpoints("wt-report-test-store",
                                   [("wt-report-test-store-abc-1", "10.0.0.2")])
        self._patch(mock.patch.object(materialiser, "_rollout_complete",
                                      lambda d: True))
        report = materialiser.materialise(template(
            edges=[{"from": "gateway", "to": "store"}],
            gateway={**AXES, "placements": [{"site": "site-B", "count": 1}]},
            store={**SINK, "placements": [{"site": "site-C", "count": 1}]}))
        self.assertEqual(report["warnings"], [])


# ── pods whose latest config was rejected ─────────────────────────────────────

class ConfigErrorsTest(unittest.TestCase):
    PODS = {"wt-t-store-abc-1": {"ready": True, "ip": "10.0.0.1"},
            "wt-t-store-abc-2": {"ready": True, "ip": "10.0.0.2"},
            "wt-t-store-abc-3": {"ready": True, "ip": "10.0.0.3"},
            "wt-t-store-abc-4": {"ready": False, "ip": None}}
    SCRAPES = {"10.0.0.1": {"worker_config_ok": 1.0},
               "10.0.0.2": {"worker_config_ok": 0.0},
               "10.0.0.3": {}}             # an older worker: no gauge

    def test_only_pods_reporting_a_rejected_config_are_listed(self):
        with mock.patch.object(controller_app.api.graph, "_scrape_pod",
                               self.SCRAPES.get):
            self.assertEqual(controller_app.api._config_errors(self.PODS),
                             ["wt-t-store-abc-2"])


# ── link shaping outcome ──────────────────────────────────────────────────────

def _ok(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0, "", "")


class NetemReportTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(netem, "_resolve_sites", lambda m: SITES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def applied_gauge(self, pair) -> float:
        return netem.LINK_APPLIED.labels(pair=pair)._value.get()

    def apply(self, links, vm_sh):
        with mock.patch.object(netem, "_vm_sh", vm_sh):
            return netem.apply(template(links=links))

    def test_missing_multipass_marks_every_link_not_applied(self):
        def no_multipass(vm, script):
            raise FileNotFoundError("multipass")
        result = self.apply([{"from": "site-B", "to": "site-C", "rtt_ms": 40}],
                            no_multipass)
        self.assertEqual(result["status"], "not_applied")
        self.assertFalse(result["links"][B_C]["applied"])
        self.assertIn("not found", result["links"][B_C]["reason"])
        self.assertEqual(self.applied_gauge(B_C), 0)
        self.assertEqual(netem.last_result(), result)

    def test_all_links_shaped(self):
        result = self.apply([{"from": "site-B", "to": "site-C", "rtt_ms": 40}],
                            _ok)
        self.assertEqual(result, {"status": "applied",
                                  "links": {B_C: {"applied": True}}})
        self.assertEqual(self.applied_gauge(B_C), 1)

    def test_a_link_needs_both_ends_shaped(self):
        # site-c's tc fails: B↔C is not in effect, A↔B still is.
        def fail_on_c(vm, script):
            if vm == "site-c":
                return subprocess.CompletedProcess(vm, 2, "", "RTNETLINK error")
            return _ok(vm)
        result = self.apply(
            [{"from": "site-A", "to": "site-B", "rtt_ms": 20},
             {"from": "site-B", "to": "site-C", "rtt_ms": 40}], fail_on_c)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["links"][A_B], {"applied": True})
        self.assertIn("'site-c'", result["links"][B_C]["reason"])
        self.assertEqual(self.applied_gauge(B_C), 0)

    def test_no_links_is_none(self):
        result = self.apply(None, _ok)
        self.assertEqual(result, {"status": "none", "links": {}})


class ShapingReportedByApiTest(ControllerTestCase):
    def test_unshaped_link_is_a_warning_and_shows_in_overview(self):
        def no_multipass(vm, script):
            raise FileNotFoundError("multipass")
        self._patch(mock.patch.object(netem, "_resolve_sites", lambda m: SITES))
        self._patch(mock.patch.object(netem, "_vm_sh", no_multipass))
        self._patch(mock.patch.object(controller_app.api, "overview",
                                      lambda: {}))
        client = TestClient(controller_app.app)
        self.assertEqual(client.post("/template", json=template()).status_code,
                         201)

        r = client.patch("/template", json={"network_links": [
            {"from": "site-B", "to": "site-C", "rtt_ms": 40}]})

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["network_shaping"]["status"], "not_applied")
        self.assertTrue(any(w.startswith(f"network link {B_C} is NOT shaped")
                            for w in body["warnings"]), body["warnings"])
        overview = client.get("/overview").json()
        self.assertEqual(overview["network_shaping"]["status"], "not_applied")


if __name__ == "__main__":
    unittest.main()
