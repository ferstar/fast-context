from __future__ import annotations

import os
import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402


def _acquire_remote_slot_in_child(result_queue: multiprocessing.Queue) -> None:
    with core._remote_search_slot():
        result_queue.put("acquired")


class RemoteSearchLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_path = str(Path(self.temp_dir.name) / "remote.lock")
        self.env = patch.dict(
            os.environ,
            {
                "WS_REMOTE_LOCK_PATH": self.lock_path,
                "WS_REMOTE_LOCK_TIMEOUT_MS": "1000",
                "WS_REMOTE_LOCK_POLL_MS": "10",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_remote_slot_serializes_concurrent_callers(self) -> None:
        second_acquired = threading.Event()
        second_finished = threading.Event()

        def enter_second_slot() -> None:
            with core._remote_search_slot():
                second_acquired.set()
            second_finished.set()

        with core._remote_search_slot():
            worker = threading.Thread(target=enter_second_slot)
            worker.start()
            time.sleep(0.05)
            self.assertFalse(second_acquired.is_set())

        worker.join(timeout=1)
        self.assertTrue(second_acquired.is_set())
        self.assertTrue(second_finished.is_set())

    def test_remote_slot_serializes_separate_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()

        with core._remote_search_slot():
            worker = context.Process(
                target=_acquire_remote_slot_in_child,
                args=(result_queue,),
            )
            worker.start()
            time.sleep(0.1)
            self.assertTrue(result_queue.empty())

        worker.join(timeout=2)
        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(result_queue.get(timeout=1), "acquired")

    def test_remote_slot_times_out_without_leaking_lock(self) -> None:
        with core._remote_search_slot():
            with patch.dict(
                os.environ,
                {"WS_REMOTE_LOCK_TIMEOUT_MS": "20"},
                clear=False,
            ):
                with self.assertRaises(core.RemoteSearchBusy):
                    with core._remote_search_slot():
                        self.fail("contended slot should not be acquired")

        with core._remote_search_slot() as waited_ms:
            self.assertGreaterEqual(waited_ms, 0)

    def test_remote_slot_releases_after_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with core._remote_search_slot():
                raise RuntimeError("boom")

        with core._remote_search_slot():
            pass


if __name__ == "__main__":
    unittest.main()
