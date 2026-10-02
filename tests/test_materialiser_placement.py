"""
Regression tests for Deployment naming across a placement change.

An app's Deployment names depend on how many placement targets it has:
`wt-<template>-<app>` for one, `wt-<template>-<app>-<i>` for several (see
materialiser._render_app). Re-materialising a template whose placement
changed target count therefore *renames* the app's Deployments, and until
materialise() learned to prune, the ones under the old names kept running:
a `store` app moved from [site-A×1, site-B×2] to [site-C×2] ended up with
5 pods on 3 nodes instead of 2 on one, all of them fronted by the app's
Service (it selects the broad {template, role}) and all of them counted as
legitimate by GET /overview.

Run from the repo root:

    python -m unittest discover -s tests

Needs Python 3.10+ (the controller annotates with `X | None`) and pyyaml
plus prometheus-client from controller/requirements.txt. Nothing else —
the Kubernetes API itself is faked, see fake_k8s.FakeCluster.
"""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "controller"))

import materialiser  # noqa: E402
import k8s  # noqa: E402

from fake_k8s import NAMESPACE, NO_LINKS, FakeCluster  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BLUEPRINT = REPO_ROOT / "manifests" / "worker-template.yaml"

TEMPLATE_NAME = "placement-test"
DEP = f"wt-{TEMPLATE_NAME}-store"          # single-target name
DEP_0, DEP_1, DEP_2 = f"{DEP}-0", f"{DEP}-1", f"{DEP}-2"

# Mirrors the cluster the bug was found on: three nodes, one site each.
NODE_SITE_MAPPING = [
    {"k8s_node": "microk8s-vm", "site_name": "site-A"},
    {"k8s_node": "site-b", "site_name": "site-B"},
    {"k8s_node": "site-c", "site_name": "site-C"},
]

# Two targets: 1 pod on site-A, 2 on site-B.
SPLIT = [{"site": "site-A", "count": 1}, {"site": "site-B", "count": 2}]
# One target: 2 pods on site-C.
SINGLE = [{"site": "site-C", "count": 2}]

HOSTNAME = materialiser.HOSTNAME_LABEL


def template(placements, extra_apps=None) -> dict:
    """A one-app template whose `store` app uses the given placements.

    No `edges`, so materialise()'s phase 2 resolves no peers and never waits
    on Endpoints — these tests are about phase 1's Deployment set.
    """
    apps = {
        "store": {
            "cpu": {"a": 10, "b": 100},
            "ram": {"a": 4, "b": 64},
            "net": {"a": 0.5, "b": 5},
            "placements": placements,
        },
    }
    apps.update(extra_apps or {})
    return {
        "name": TEMPLATE_NAME,
        "x": 10,
        "node_site_mapping": NODE_SITE_MAPPING,
        "apps": apps,
    }


class PlacementChangeTest(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        self._patch(mock.patch.object(k8s, "request", self.cluster.request))
        self._patch(mock.patch.object(k8s, "namespace", lambda: NAMESPACE))
        self._patch(mock.patch.object(materialiser, "BLUEPRINT_PATH",
                                      str(BLUEPRINT)))
        # DEFAULT_NODE collapses every placement into a single Deployment on
        # that node, which is exactly what these tests must not do. It is read
        # from the environment at import time, so pin it off here rather than
        # depending on how the developer's shell is set up.
        self._patch(mock.patch.object(materialiser, "DEFAULT_NODE", ""))
        # Link shaping runs `multipass exec` against real nodes and is
        # documented as auxiliary; irrelevant to the Deployment set.
        self._patch(mock.patch.object(materialiser.netem, "apply",
                                      lambda tmpl: NO_LINKS))

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertDeployments(self, *names: str):
        self.assertEqual(self.cluster.names("deployments"), set(names))

    # ── the two directions of a target-count change ───────────────────────

    def test_many_targets_to_one_prunes_the_suffixed_deployments(self):
        materialiser.materialise(template(SPLIT))
        self.assertDeployments(DEP_0, DEP_1)
        self.assertEqual(self.cluster.replicas(DEP_0), 1)
        self.assertEqual(self.cluster.replicas(DEP_1), 2)

        materialiser.materialise(template(SINGLE))

        # The unsuffixed Deployment is now the only one: no `-0`/`-1` left
        # running pods on microk8s-vm and site-b.
        self.assertDeployments(DEP)
        self.assertEqual(self.cluster.replicas(DEP), 2)
        self.assertEqual(self.cluster.node_selector(DEP), {HOSTNAME: "site-c"})
        self.assertEqual(sorted(self.cluster.deleted("deployments")),
                         [DEP_0, DEP_1])

    def test_one_target_to_many_prunes_the_unsuffixed_deployment(self):
        materialiser.materialise(template(SINGLE))
        self.assertDeployments(DEP)
        self.assertEqual(self.cluster.replicas(DEP), 2)

        materialiser.materialise(template(SPLIT))

        self.assertDeployments(DEP_0, DEP_1)
        self.assertEqual(self.cluster.replicas(DEP_0), 1)
        self.assertEqual(self.cluster.replicas(DEP_1), 2)
        self.assertEqual(self.cluster.node_selector(DEP_0),
                         {HOSTNAME: "microk8s-vm"})
        self.assertEqual(self.cluster.node_selector(DEP_1),
                         {HOSTNAME: "site-b"})
        self.assertEqual(self.cluster.deleted("deployments"), [DEP])

    def test_shrinking_a_multi_target_app_prunes_only_the_dropped_target(self):
        three = SPLIT + [{"site": "site-C", "count": 3}]
        materialiser.materialise(template(three))
        self.assertDeployments(DEP_0, DEP_1, DEP_2)

        materialiser.materialise(template(SPLIT))

        self.assertDeployments(DEP_0, DEP_1)
        self.assertEqual(self.cluster.deleted("deployments"), [DEP_2])

    # ── the prune must not overreach ──────────────────────────────────────

    def test_re_materialising_unchanged_deletes_nothing(self):
        materialiser.materialise(template(SPLIT))
        materialiser.materialise(template(SPLIT))

        self.assertDeployments(DEP_0, DEP_1)
        self.assertEqual(self.cluster.deleted("deployments"), [])

    def test_prune_is_scoped_to_the_app_not_its_name_prefix(self):
        # `store-cache`'s single-target Deployment is named wt-<t>-store-cache,
        # which a name-prefix match would read as one of `store`'s children.
        # Only the `role` label distinguishes them.
        cache = {"store-cache": {"cpu": {"a": 1, "b": 10},
                                 "ram": {"a": 1, "b": 10},
                                 "net": {"a": 0.1, "b": 1},
                                 "placements": SINGLE}}
        materialiser.materialise(template(SPLIT, extra_apps=cache))
        cache_dep = f"wt-{TEMPLATE_NAME}-store-cache"
        self.assertDeployments(DEP_0, DEP_1, cache_dep)

        materialiser.materialise(template(SINGLE, extra_apps=cache))

        self.assertDeployments(DEP, cache_dep)
        self.assertNotIn(cache_dep, self.cluster.deleted("deployments"))

    def test_the_apps_service_and_configmap_are_not_pruned(self):
        materialiser.materialise(template(SPLIT))
        materialiser.materialise(template(SINGLE))

        # One Service + one ConfigMap per app, shared by every Deployment —
        # the rename only ever affects Deployments.
        self.assertEqual(self.cluster.names("services"), {DEP})
        self.assertEqual(self.cluster.names("configmaps"), {f"{DEP}-config"})
        self.assertEqual(self.cluster.deleted("services"), [])
        self.assertEqual(self.cluster.deleted("configmaps"), [])


if __name__ == "__main__":
    unittest.main()
