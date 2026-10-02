"""
Regression tests for resolving peer IPs after a re-materialise.

Phase 2 of materialise() writes each upstream app the pod IPs behind its
downstream Services. It used to accept the first `count` Ready addresses it
saw — but right after a re-materialise those are still the *previous* pods:
the pods of a just-pruned Deployment, or of an older ReplicaSet mid rolling
update, stay Ready until they terminate. Found on the live cluster: moving
`store` from [site-B×1, site-C×2] to [site-C×2] left `gateway` streaming at
the two deleted site-C pods' IPs, retrying every 5 s, with the gateway→store
edge at 0 Mbps until the next PATCH.

Run from the repo root:

    python -m unittest discover -s tests

The cluster is faked (fake_k8s.FakeCluster) and so is the clock: each
`time.sleep` in the materialiser's wait loop is one tick, and the test
scripts what the Endpoints / rollout status show at each tick.
"""

import json
import pathlib
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "controller"))

import materialiser  # noqa: E402
import k8s  # noqa: E402

from fake_k8s import NAMESPACE, FakeCluster  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BLUEPRINT = REPO_ROOT / "manifests" / "worker-template.yaml"

NAME = "peer-test"
GATEWAY = f"wt-{NAME}-gateway"
STORE = f"wt-{NAME}-store"                 # single-target Deployment + Service
STORE_0, STORE_1 = f"{STORE}-0", f"{STORE}-1"

NODE_SITE_MAPPING = [
    {"k8s_node": "microk8s-vm", "site_name": "site-A"},
    {"k8s_node": "site-b", "site_name": "site-B"},
    {"k8s_node": "site-c", "site_name": "site-C"},
]

GATEWAY_POD = (f"{GATEWAY}-6c54c67849-bblfm", "10.1.0.1")
# store as [site-A×1, site-B×2]: one pod from store-0, two from store-1.
SPLIT_PODS = [(f"{STORE_0}-7dfdb47d68-fk46h", "10.1.1.1"),
              (f"{STORE_1}-8584b97874-bwch5", "10.1.1.2"),
              (f"{STORE_1}-8584b97874-l42rp", "10.1.1.3")]
# store as [site-C×2]: the replacement pods, under the unsuffixed name.
NEW_PODS = [(f"{STORE}-bcb468c7f-bp2w9", "10.1.2.1"),
            (f"{STORE}-bcb468c7f-vfxtt", "10.1.2.2")]


def template(store_placements) -> dict:
    """gateway → store, with store placed by `store_placements`."""
    axes = {"cpu": {"a": 5, "b": 50}, "ram": {"a": 1, "b": 64},
            "net": {"a": 0.1, "b": 1}}
    return {
        "name": NAME,
        "x": 10,
        "node_site_mapping": NODE_SITE_MAPPING,
        "apps": {
            "gateway": {**axes, "placements": [{"site": "site-B", "count": 1}]},
            "store": {**axes, "placements": store_placements},
        },
        "edges": [{"from": "gateway", "to": "store"}],
    }


SPLIT = [{"site": "site-A", "count": 1}, {"site": "site-B", "count": 2}]
ON_C = [{"site": "site-C", "count": 2}]
ON_B = [{"site": "site-B", "count": 2}]


class FakeClock:
    """Stands in for the materialiser's `time` module. Every sleep() is one
    tick; the step scripted for that tick (if any) runs before it returns."""

    def __init__(self):
        self.now = 0.0
        self.ticks = 0
        self.script: dict[int, callable] = {}

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.ticks += 1
        step = self.script.pop(self.ticks, None)
        if step:
            step()

    def run(self, script: dict) -> None:
        """Restart the tick count and script the ticks that follow."""
        self.ticks = 0
        self.script = dict(script)


class PeerResolutionTest(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        self.clock = FakeClock()
        self._patch(mock.patch.object(k8s, "request", self.cluster.request))
        self._patch(mock.patch.object(k8s, "namespace", lambda: NAMESPACE))
        self._patch(mock.patch.object(materialiser, "BLUEPRINT_PATH",
                                      str(BLUEPRINT)))
        self._patch(mock.patch.object(materialiser, "DEFAULT_NODE", ""))
        self._patch(mock.patch.object(materialiser.netem, "apply",
                                      lambda tmpl: None))
        self._patch(mock.patch.object(
            materialiser, "time",
            types.SimpleNamespace(monotonic=self.clock.monotonic,
                                  sleep=self.clock.sleep)))

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def gateway_peers(self) -> list[str]:
        cm = self.cluster.objects["configmaps"][f"{GATEWAY}-config"]
        return json.loads(cm["data"]["config.json"])["peers"]

    def bring_up(self, store_placements, store_pods):
        """Materialise and let every Deployment come up with the given pods."""
        self.cluster.set_endpoints(GATEWAY, [GATEWAY_POD])
        self.cluster.set_endpoints(STORE, store_pods)

        def roll_out_all():
            for dep in self.cluster.names("deployments"):
                self.cluster.roll_out(dep)

        self.clock.run({1: roll_out_all})
        materialiser.materialise(template(store_placements))
        self.assertEqual(sorted(self.gateway_peers()),
                         sorted(ip for _, ip in store_pods))

    # ── the bug as found: a placement change renames store's Deployments ──

    def test_pruned_deployments_pods_are_never_handed_out_as_peers(self):
        self.bring_up(SPLIT, SPLIT_PODS)

        # After the re-apply, the old pods are still Ready in the Endpoints
        # (pruning is asynchronous). The new pods join them, and only then
        # does the new Deployment report its rollout done.
        self.clock.run({
            1: lambda: self.cluster.set_endpoints(STORE, SPLIT_PODS + NEW_PODS),
            2: lambda: self.cluster.roll_out(STORE),
        })
        materialiser.materialise(template(ON_C))

        self.assertEqual(sorted(self.gateway_peers()),
                         sorted(ip for _, ip in NEW_PODS))

    def test_waits_out_a_rolling_update_of_the_same_deployment(self):
        # One target before and after, so the Deployment keeps its name and
        # Kubernetes rolls it: old and new pods share an owner for a while.
        old = [(f"{STORE}-aaaaaaaaa-old01", "10.1.1.1"),
               (f"{STORE}-aaaaaaaaa-old02", "10.1.1.2")]
        self.bring_up(ON_C, old)

        self.clock.run({
            1: lambda: self.cluster.set_endpoints(STORE, old + NEW_PODS[:1]),
            2: lambda: (self.cluster.set_endpoints(STORE, old + NEW_PODS),
                        self.cluster.roll_out(STORE)),
            3: lambda: self.cluster.set_endpoints(STORE, NEW_PODS),
        })
        materialiser.materialise(template(ON_B))

        self.assertEqual(sorted(self.gateway_peers()),
                         sorted(ip for _, ip in NEW_PODS))

    def test_timeout_falls_back_to_current_pods_only(self):
        # One replacement pod never becomes Ready (e.g. unschedulable). After
        # the wait times out, the Ready one is used — never the old pods.
        self.bring_up(SPLIT, SPLIT_PODS)

        self.clock.run({
            1: lambda: self.cluster.set_endpoints(STORE,
                                                  SPLIT_PODS + NEW_PODS[:1]),
        })
        materialiser.materialise(template(ON_C))

        self.assertEqual(self.gateway_peers(), [NEW_PODS[0][1]])

    def test_unchanged_re_materialise_resolves_without_waiting(self):
        self.bring_up(SPLIT, SPLIT_PODS)

        self.clock.run({})
        materialiser.materialise(template(SPLIT))

        self.assertEqual(self.clock.ticks, 0)
        self.assertEqual(sorted(self.gateway_peers()),
                         sorted(ip for _, ip in SPLIT_PODS))


if __name__ == "__main__":
    unittest.main()
