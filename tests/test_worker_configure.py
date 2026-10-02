"""
Regression tests for the worker's configure() on a malformed payload.

configure() used to stop the running load first and parse the payload
second, so a bad value (here: a string CPU coefficient that a PATCH let
through) raised after stress-ng, the RAM buffer and iperf3 were already
gone. The pod sat idle — still Ready, still exporting its old targets —
until a good config arrived.

Run from the repo root:

    python -m unittest discover -s tests

Needs the worker's runtime deps (prometheus-client, psutil, watchdog — see
worker/Dockerfile). No load generator is ever started: loads.* is mocked.
"""

import pathlib
import sys
import unittest
from unittest import mock

WORKER_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "worker")

# The worker and the controller both have a top-level `watcher` module.
# Import the worker's with its own directory first on the path, then put
# the controller's back so other test modules keep seeing theirs.
_saved = {m: sys.modules.pop(m) for m in ("watcher",) if m in sys.modules}
sys.path.insert(0, WORKER_DIR)
try:
    import worker  # noqa: E402
finally:
    sys.path.remove(WORKER_DIR)
    sys.modules.pop("watcher", None)
    sys.modules.update(_saved)

GOOD = {"x": 3.2,
        "cpu": {"a": 5, "b": 50},
        "ram": {"a": 1, "b": 64},
        "net": {"a": 0.1, "b": 1},
        "peers": ["10.1.24.14"]}


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        self.loads = mock.Mock()
        patcher = mock.patch.object(worker, "loads", self.loads)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_malformed_coefficient_leaves_the_running_load_alone(self):
        bad = {**GOOD, "cpu": {"a": "fast", "b": 50}}
        with self.assertRaises(ValueError):
            worker.configure(bad)
        self.assertEqual(self.loads.mock_calls, [])

    def test_missing_field_leaves_the_running_load_alone(self):
        bad = {k: v for k, v in GOOD.items() if k != "ram"}
        with self.assertRaises(KeyError):
            worker.configure(bad)
        self.assertEqual(self.loads.mock_calls, [])

    def test_malformed_port_offset_leaves_the_running_load_alone(self):
        bad = {**GOOD, "port_offset_by_pod": {worker.POD_NAME: "one"}}
        with self.assertRaises(ValueError):
            worker.configure(bad)
        self.assertEqual(self.loads.mock_calls, [])

    def test_valid_payload_stops_then_starts_the_new_load(self):
        worker.configure(GOOD)
        self.assertEqual([c[0] for c in self.loads.mock_calls],
                         ["stop_current", "start_network", "start_cpu"])
        self.assertEqual(self.loads.start_cpu.call_args.args[0], 66.0)


if __name__ == "__main__":
    unittest.main()
