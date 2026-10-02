"""
Regression tests for PATCH /template accepting values POST would reject.

POST validates its body against the Pydantic Template model; PATCH bodies
are free-form (dot-paths, partial objects) and used to get only the graph
checks in materialiser.validate(). Found on the live cluster:
`{"apps.store.cpu.a": "fast"}` returned 200, was stored, reached the store
workers' ConfigMap, and every store pod stopped generating load — while
still reporting its old targets and staying Ready.

Run from the repo root:

    python -m unittest discover -s tests

Needs fastapi + httpx (TestClient) on top of controller/requirements.txt.
"""

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "controller"))

from fastapi.testclient import TestClient  # noqa: E402

import app as controller_app  # noqa: E402
import k8s  # noqa: E402
import materialiser  # noqa: E402

from fake_k8s import NAMESPACE, NO_LINKS, FakeCluster  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BLUEPRINT = REPO_ROOT / "manifests" / "worker-template.yaml"

NAME = "patch-test"
STORE_CM = f"wt-{NAME}-store-config"

# No edges: phase 2 resolves no peers, so nothing waits on Endpoints.
TEMPLATE = {
    "name": NAME,
    "x": 20,
    "apps": {
        "store": {"count": 2,
                  "cpu": {"a": 5, "b": 50},
                  "ram": {"a": 1, "b": 64},
                  "net": {"a": 0.1, "b": 1}},
    },
}


class PatchValidationTest(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        for patcher in (
                mock.patch.object(k8s, "request", self.cluster.request),
                mock.patch.object(k8s, "namespace", lambda: NAMESPACE),
                mock.patch.object(materialiser, "BLUEPRINT_PATH", str(BLUEPRINT)),
                mock.patch.object(materialiser, "DEFAULT_NODE", ""),
                mock.patch.object(materialiser.netem, "apply",
                                      lambda tmpl: NO_LINKS),
                mock.patch.object(materialiser.netem, "teardown",
                                  lambda *a, **kw: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Not used as a context manager, so the lifespan (ConfigMap watcher
        # thread, runner resume) never starts.
        self.client = TestClient(controller_app.app)
        r = self.client.post("/template", json=TEMPLATE)
        self.assertEqual(r.status_code, 201, r.text)

    def stored(self) -> dict:
        t = self.client.get("/template").json()
        t.pop("timestamp")
        return t

    def worker_config(self) -> dict:
        cm = self.cluster.objects["configmaps"][STORE_CM]
        return json.loads(cm["data"]["config.json"])

    def assertRejected(self, patch: dict, *fragments: str):
        before, config_before = self.stored(), self.worker_config()
        r = self.client.patch("/template", json=patch)
        self.assertEqual(r.status_code, 400, r.text)
        for fragment in fragments:
            self.assertIn(fragment, r.json()["error"])
        # Nothing changed: not the stored template, not what workers read.
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.worker_config(), config_before)

    # ── values the POST schema rejects ────────────────────────────────────

    def test_string_coefficient_is_rejected(self):
        self.assertRejected({"apps.store.cpu.a": "fast"},
                            "apps.store.cpu.a")

    def test_malformed_edge_is_a_400_not_a_500(self):
        self.assertRejected({"edges": [1]}, "edges.0")

    def test_zero_count_is_rejected(self):
        self.assertRejected({"apps.store.count": 0}, "apps.store.count")

    # ── valid patches behave as before ────────────────────────────────────

    def test_valid_dot_path_patch_still_applies(self):
        r = self.client.patch("/template", json={"apps.store.cpu.a": 7})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.stored()["apps"]["store"]["cpu"],
                         {"a": 7.0, "b": 50.0})
        self.assertEqual(self.worker_config()["cpu"], {"a": 7.0, "b": 50.0})

    def test_patch_stores_what_post_would(self):
        # A numeric string is coerced by the POST schema; a PATCH now stores
        # the same coerced value instead of the raw string.
        r = self.client.patch("/template", json={"x": "40"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.stored()["x"], 40.0)


class ValidateCoefficientsTest(unittest.TestCase):
    """materialiser.validate() is also the only check on the declarative
    (labelled-ConfigMap) path, which never sees the Pydantic model."""

    def test_non_numeric_coefficients_are_rejected(self):
        for bad in ("fast", "5", None, True, float("nan"), float("inf")):
            with self.subTest(value=bad):
                t = json.loads(json.dumps(TEMPLATE))
                t["apps"]["store"]["ram"]["b"] = bad
                with self.assertRaisesRegex(ValueError, r"store'\.ram\.b"):
                    materialiser.validate(t)

    def test_numeric_coefficients_pass(self):
        t = json.loads(json.dumps(TEMPLATE))
        t["apps"]["store"]["ram"] = {"a": 0, "b": 64.5}
        materialiser.validate(t)


if __name__ == "__main__":
    unittest.main()
