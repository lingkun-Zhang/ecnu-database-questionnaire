"""Standard-library tests for the real runner's local graceful-stop protocol."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("project_runner", Path(__file__).parents[1] / "scripts" / "run.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RunnerControlTests(unittest.TestCase):
    def setUp(self):
        # Keep disposable files under the project on restricted Windows runners.
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-test-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / ".runtime"
        self.runtime.mkdir()
        self.instance = runner.make_instance(Path(self.temporary.name))
        self.request = {"pid": self.instance["pid"], "instanceToken": self.instance["instanceToken"]}
        self.instance_path = self.runtime / "backend.instance.json"
        self.stop_path = self.runtime / "backend.stop.json"

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_instance_records_current_process_and_absolute_paths(self):
        self.assertEqual(self.instance["pid"], os.getpid())
        self.assertEqual(self.instance["launcherPid"], os.getppid())
        for key in ("projectRoot", "runPath", "pythonPath", "basePythonPath"):
            self.assertTrue(Path(self.instance[key]).is_absolute(), key)
        self.assertTrue(self.instance["startedAt"].endswith("Z"))
        self.assertEqual(len(self.instance["instanceToken"]), 43)
        self.assertNotEqual(self.instance["instanceToken"], runner.make_instance()["instanceToken"])

    def test_matching_stop_is_accepted(self):
        self.assertTrue(runner.matches_stop(self.request, self.instance))

    def test_wrong_pid_is_rejected(self):
        self.assertFalse(runner.matches_stop({**self.request, "pid": self.instance["pid"] + 1}, self.instance))

    def test_wrong_token_is_rejected(self):
        self.assertFalse(runner.matches_stop({**self.request, "instanceToken": "other"}, self.instance))

    def test_unknown_fields_are_rejected(self):
        self.assertFalse(runner.matches_stop({**self.request, "force": True}, self.instance))

    def test_missing_and_wrong_types_are_rejected(self):
        for value in (None, [], {}, {"pid": self.instance["pid"]},
                      {**self.request, "pid": str(self.instance["pid"])},
                      {**self.request, "pid": True}, {**self.request, "instanceToken": 42},
                      {**self.request, "instanceToken": "非 ASCII 令牌"},
                      {**self.request, "instanceToken": ""}):
            with self.subTest(value=value):
                self.assertFalse(runner.matches_stop(value, self.instance))

    def test_missing_control_file_is_ignored(self):
        self.assertIsNone(runner.read_control(self.stop_path))

    def test_malformed_and_nonobject_files_are_ignored(self):
        for value in (b"", b"{", b"null", b"[]", b"\xff", b'{"a": NaN}', b'{"a":1,"a":2}'):
            with self.subTest(value=value):
                self.stop_path.write_bytes(value)
                self.assertIsNone(runner.read_control(self.stop_path))

    def test_exact_size_limit_is_accepted_but_larger_file_is_ignored(self):
        value = json.dumps(self.request).encode("utf-8")
        self.stop_path.write_bytes(value + b" " * (runner.MAX_CONTROL_BYTES - len(value)))
        self.assertEqual(runner.read_control(self.stop_path), self.request)
        self.stop_path.write_bytes(value + b" " * (runner.MAX_CONTROL_BYTES + 1 - len(value)))
        self.assertIsNone(runner.read_control(self.stop_path))

    def test_utf8_bom_from_powershell_is_accepted(self):
        self.stop_path.write_text(json.dumps(self.request), encoding="utf-8-sig")
        self.assertEqual(runner.read_control(self.stop_path), self.request)

    def test_directory_in_place_of_control_file_is_ignored(self):
        self.stop_path.mkdir()
        self.assertIsNone(runner.read_control(self.stop_path))

    def test_publish_is_complete_and_does_not_leave_temporary_files(self):
        runner.publish_instance(self.runtime, self.instance)
        self.assertEqual(runner.read_control(self.instance_path), self.instance)
        self.assertEqual(list(self.runtime.glob("*.tmp")), [])

    def test_cleanup_removes_only_own_record_and_stop(self):
        runner.publish_instance(self.runtime, self.instance)
        self.write(self.stop_path, self.request)
        self.assertEqual(runner.cleanup_instance(self.runtime, self.instance), (True, True))
        self.assertFalse(self.instance_path.exists())
        self.assertFalse(self.stop_path.exists())

    def test_cleanup_preserves_new_instance_and_its_stop(self):
        replacement = runner.make_instance()
        runner.publish_instance(self.runtime, replacement)
        new_request = {"pid": replacement["pid"], "instanceToken": replacement["instanceToken"]}
        self.write(self.stop_path, new_request)
        self.assertEqual(runner.cleanup_instance(self.runtime, self.instance), (False, False))
        self.assertEqual(runner.read_control(self.instance_path), replacement)
        self.assertEqual(runner.read_control(self.stop_path), new_request)

    def test_cleanup_preserves_modified_record_even_with_same_pid_and_token(self):
        modified = {**self.instance, "startedAt": "replacement"}
        self.write(self.instance_path, modified)
        self.assertEqual(runner.cleanup_instance(self.runtime, self.instance), (False, False))
        self.assertEqual(runner.read_control(self.instance_path), modified)

    def test_cleanup_preserves_unknown_or_malformed_stop(self):
        for value in (json.dumps({**self.request, "force": True}), "{"):
            with self.subTest(value=value):
                self.stop_path.write_text(value, encoding="utf-8")
                runner.cleanup_instance(self.runtime, self.instance)
                self.assertEqual(self.stop_path.read_text(encoding="utf-8"), value)

    def test_cleanup_preserves_record_replaced_during_verification(self):
        self.write(self.instance_path, self.instance)
        original = runner._read_control_with_identity
        replacement = runner.make_instance()

        def replace_after_read(path):
            result = original(path)
            if path == self.instance_path:
                temporary = self.runtime / "replacement.json"
                self.write(temporary, replacement)
                os.replace(temporary, path)
            return result

        with mock.patch.object(runner, "_read_control_with_identity", side_effect=replace_after_read):
            self.assertEqual(runner.cleanup_instance(self.runtime, self.instance), (False, False))
        self.assertEqual(runner.read_control(self.instance_path), replacement)

    def test_watch_sets_should_exit_for_matching_request(self):
        self.write(self.stop_path, self.request)
        server = SimpleNamespace(should_exit=False)
        runner.watch_stop(server, self.runtime, self.instance, threading.Event())
        self.assertTrue(server.should_exit)

    def test_watch_ignores_residual_signal_for_previous_instance(self):
        self.write(self.stop_path, {**self.request, "instanceToken": "previous-instance"})
        server = SimpleNamespace(should_exit=False)
        finished = threading.Event()
        original_wait = finished.wait

        def finish_after_poll(timeout):
            self.assertEqual(timeout, runner.POLL_SECONDS)
            finished.set()
            return original_wait(0)

        with mock.patch.object(finished, "wait", side_effect=finish_after_poll):
            runner.watch_stop(server, self.runtime, self.instance, finished)
        self.assertFalse(server.should_exit)
        self.assertTrue(self.stop_path.exists())

    def test_run_server_publishes_before_serving_and_cleans_after_graceful_exit(self):
        server = SimpleNamespace(should_exit=False)
        observations = []

        def serve():
            instance = runner.read_control(self.instance_path)
            self.assertIsNotNone(instance)
            self.assertEqual(instance["pid"], os.getpid())
            self.assertEqual(instance["projectRoot"], str(Path(self.temporary.name).resolve()))
            observations.append("started")
            self.write(self.stop_path, {"pid": instance["pid"], "instanceToken": instance["instanceToken"]})
            deadline = time.monotonic() + 2
            while not server.should_exit and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            self.assertTrue(server.should_exit)
            observations.append("graceful")

        server.run = serve
        fake_uvicorn = SimpleNamespace(Config=mock.Mock(return_value=object()), Server=mock.Mock(return_value=server))
        with mock.patch.object(runner, "ROOT", Path(self.temporary.name)), \
                mock.patch.object(runner.sys, "path", list(runner.sys.path)), \
                mock.patch.dict(runner.sys.modules, {"uvicorn": fake_uvicorn}):
            runner.run_server()
        self.assertEqual(observations, ["started", "graceful"])
        self.assertFalse(self.instance_path.exists())
        self.assertFalse(self.stop_path.exists())
        self.assertFalse(any(thread.name == "backend-stop" for thread in threading.enumerate()))
        fake_uvicorn.Config.assert_called_once_with("app.main:app", host="127.0.0.1", port=8000, log_level="info")

    def test_run_server_cleans_own_record_when_server_raises(self):
        server = SimpleNamespace(should_exit=False, run=mock.Mock(side_effect=RuntimeError("startup failed")))
        fake_uvicorn = SimpleNamespace(Config=mock.Mock(return_value=object()), Server=mock.Mock(return_value=server))
        with mock.patch.object(runner, "ROOT", Path(self.temporary.name)), \
                mock.patch.object(runner.sys, "path", list(runner.sys.path)), \
                mock.patch.dict(runner.sys.modules, {"uvicorn": fake_uvicorn}):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                runner.run_server()
        self.assertFalse(self.instance_path.exists())
        self.assertFalse(any(thread.name == "backend-stop" for thread in threading.enumerate()))

    def test_control_link_refusal_branch_preserves_file_without_link_privileges(self):
        self.write(self.stop_path, self.request)
        original = runner._is_link
        with mock.patch.object(runner, "_is_link", side_effect=lambda path: path == self.stop_path or original(path)):
            self.assertIsNone(runner.read_control(self.stop_path))
            self.assertEqual(runner.cleanup_instance(self.runtime, self.instance), (False, False))
        self.assertEqual(runner.read_control(self.stop_path), self.request)

    def test_runtime_link_refusal_branch_does_not_write_without_link_privileges(self):
        original = runner._is_link
        with mock.patch.object(runner, "_is_link", side_effect=lambda path: path == self.runtime or original(path)):
            with self.assertRaises(OSError):
                runner.publish_instance(self.runtime, self.instance)
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_instance_link_refusal_branch_preserves_file_without_link_privileges(self):
        self.write(self.instance_path, {"preserve": True})
        original = runner._is_link
        with mock.patch.object(runner, "_is_link", side_effect=lambda path: path == self.instance_path or original(path)):
            with self.assertRaises(OSError):
                runner.publish_instance(self.runtime, self.instance)
        self.assertEqual(runner.read_control(self.instance_path), {"preserve": True})

    def create_symlink_or_skip(self, link, destination, directory=False):
        try:
            link.symlink_to(destination, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symlinks unavailable: {type(error).__name__}")

    def test_linked_control_file_is_neither_read_nor_deleted(self):
        outside = Path(self.temporary.name) / "outside.json"
        self.write(outside, self.request)
        self.create_symlink_or_skip(self.stop_path, outside)
        self.assertIsNone(runner.read_control(self.stop_path))
        runner.cleanup_instance(self.runtime, self.instance)
        self.assertTrue(self.stop_path.is_symlink())
        self.assertEqual(runner.read_control(outside), self.request)

    def test_linked_instance_record_is_not_overwritten(self):
        outside = Path(self.temporary.name) / "outside.json"
        self.write(outside, {"preserve": True})
        self.create_symlink_or_skip(self.instance_path, outside)
        with self.assertRaises(OSError):
            runner.publish_instance(self.runtime, self.instance)
        self.assertEqual(runner.read_control(outside), {"preserve": True})

    def test_linked_runtime_is_not_used(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        linked = Path(self.temporary.name) / "runtime-link"
        self.create_symlink_or_skip(linked, outside, directory=True)
        with self.assertRaises(OSError):
            runner.publish_instance(linked, self.instance)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
