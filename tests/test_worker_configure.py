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

    def test_rejected_config_leaves_config_ok_at_its_last_value(self):
        worker.configure(GOOD)
        self.assertEqual(worker.metrics.CONFIG_OK._value.get(), 1)
        worker.metrics.config_rejected("bad config: x")
        self.assertEqual(worker.metrics.CONFIG_OK._value.get(), 0)
        worker.configure(GOOD)
        self.assertEqual(worker.metrics.CONFIG_OK._value.get(), 1)

    def test_sink_reports_a_net_target_of_zero(self):
        worker.configure({**GOOD, "peers": []})
        self.assertEqual(worker.metrics.TARGET_NET._value.get(), 0)
        # Its CPU/RAM targets are unaffected.
        self.assertEqual(worker.metrics.TARGET_CPU._value.get(), 66.0)

    def test_sender_reports_its_net_formula(self):
        worker.configure(GOOD)
        self.assertAlmostEqual(worker.metrics.TARGET_NET._value.get(), 1.32)

    def test_valid_payload_stops_then_starts_the_new_load(self):
        worker.configure(GOOD)
        self.assertEqual([c[0] for c in self.loads.mock_calls],
                         ["stop_current", "start_network", "start_cpu"])
        self.assertEqual(self.loads.start_cpu.call_args.args[0], 66.0)


class WatcherErrorHookTest(unittest.TestCase):
    """Every way a config on disk can fail to apply reaches on_error, so the
    worker can export it (worker_config_ok) instead of only logging it."""

    def load(self, content: str, callback) -> list[str]:
        import tempfile
        errors: list[str] = []
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            f.write(content)
            f.flush()
            worker.watcher._load_and_apply(f.name, callback, errors.append)
        return errors

    def test_unparseable_config(self):
        errors = self.load("{not json", mock.Mock())
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("bad config"))

    def test_config_missing_a_field(self):
        errors = self.load('{"x": 1, "cpu": {"a": 1, "b": 1}}', mock.Mock())
        self.assertEqual(len(errors), 1)

    def test_callback_raising(self):
        errors = self.load(
            '{"x": 1, "cpu": {"a": "fast", "b": 1}, "ram": {"a": 1, "b": 1},'
            ' "net": {"a": 0, "b": 0}}',
            mock.Mock(side_effect=ValueError("could not convert")))
        self.assertEqual(len(errors), 1)
        self.assertIn("could not convert", errors[0])

    def test_applied_config_is_not_an_error(self):
        callback = mock.Mock()
        errors = self.load('{"x": 1, "cpu": {"a": 1, "b": 1}, '
                           '"ram": {"a": 1, "b": 1}, "net": {"a": 0, "b": 0}}',
                           callback)
        self.assertEqual(errors, [])
        callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
