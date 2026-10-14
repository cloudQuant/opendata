"""Pure dispatch checks for A2 mypy worker scheduling."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.quality import a2_check


class TestMypyWorkerDispatch(unittest.TestCase):
    """Verify deterministic scheduling without invoking mypy or the A2 gate."""

    def test_parallel_chunks_cover_sorted_unique_targets_and_isolate_caches(self) -> None:
        targets = [
            "z/last.py",
            "a/first.py",
            "d/fourth.py",
            "b/second.py",
            "c/third.py",
            "e/fifth.py",
            "f/sixth.py",
            "a/first.py",
        ]
        calls: list[list[str]] = []
        calls_lock = threading.Lock()
        worker_barrier = threading.Barrier(4, timeout=3)
        started_workers: set[str] = set()

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            with calls_lock:
                calls.append(list(args))
                cache_dir = args[args.index("--cache-dir") + 1]
                is_first_target = cache_dir not in started_workers
                started_workers.add(cache_dir)
            if is_first_target:
                worker_barrier.wait()
            path = args[-1]
            return 0, f"out:{path}\n", f"err:{path}\n"

        with patch.object(a2_check, "_run_streams", side_effect=fake_run):
            passed, results = a2_check._run_mypy_targets(targets, workers=4)

        expected = sorted(set(targets))
        self.assertTrue(passed)
        self.assertEqual([result.path for result in results], expected)
        self.assertEqual(sorted(args[-1] for args in calls), expected)
        self.assertEqual(len(calls), len(expected))

        cache_by_target = {args[-1]: args[args.index("--cache-dir") + 1] for args in calls}
        self.assertEqual(set(cache_by_target), set(expected))
        self.assertEqual(len(set(cache_by_target.values())), 4)
        self.assertEqual(
            {cache.rsplit("/", maxsplit=1)[-1] for cache in cache_by_target.values()},
            {f"worker-{worker_index:03d}" for worker_index in range(4)},
        )
        self.assertEqual(
            len({cache.rsplit("/", maxsplit=1)[0] for cache in cache_by_target.values()}),
            1,
        )

        chunks = a2_check._mypy_target_chunks(expected, 4)
        self.assertEqual(
            chunks,
            [
                (0, ("a/first.py", "b/second.py")),
                (1, ("c/third.py", "d/fourth.py")),
                (2, ("e/fifth.py", "f/sixth.py")),
                (3, ("z/last.py",)),
            ],
        )
        for worker_index, chunk in chunks:
            assigned_caches = {cache_by_target[path] for path in chunk}
            self.assertEqual(
                {cache.rsplit("/", maxsplit=1)[-1] for cache in assigned_caches},
                {f"worker-{worker_index:03d}"},
            )

        report = a2_check._mypy_results_text(results)
        self.assertEqual(report, a2_check._mypy_results_text(results))
        self.assertLess(report.index("a/first.py"), report.index("z/last.py"))

    def test_mixed_results_and_invocation_exception_fail_without_dropping_targets(self) -> None:
        targets = ["a/ok.py", "b/raises.py", "c/nonzero.py", "d/also_ok.py"]
        calls: list[str] = []
        calls_lock = threading.Lock()

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            path = args[-1]
            with calls_lock:
                calls.append(path)
            if path == "b/raises.py":
                raise OSError("mock executable failure")
            if path == "c/nonzero.py":
                return 2, "type error\n", "tool warning\n"
            return 0, f"stdout:{path}\n", f"stderr:{path}\n"

        with patch.object(a2_check, "_run_streams", side_effect=fake_run):
            passed, results = a2_check._run_mypy_targets(targets, workers=2)

        self.assertFalse(passed)
        self.assertEqual([result.path for result in results], sorted(targets))
        self.assertEqual(sorted(calls), sorted(targets))
        self.assertEqual([result.returncode for result in results], [0, 127, 2, 0])
        self.assertEqual(results[0].stdout, "stdout:a/ok.py\n")
        self.assertEqual(results[1].stdout, "")
        self.assertEqual(results[1].stderr, "mypy target invocation failed (OSError)\n")
        self.assertNotIn("mock executable failure", results[1].stderr)
        self.assertEqual(results[2].stdout, "type error\n")
        self.assertEqual(results[2].stderr, "tool warning\n")
        self.assertEqual(results[3].stderr, "stderr:d/also_ok.py\n")

    def test_one_worker_preserves_legacy_arguments_and_cli_default(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            calls.append(list(args))
            return 0, "", ""

        targets = ["z/last.py", "a/first.py"]
        with patch.object(a2_check, "_run_streams", side_effect=fake_run):
            passed, results = a2_check._run_mypy_targets(targets)

        self.assertTrue(passed)
        self.assertEqual([result.path for result in results], ["a/first.py", "z/last.py"])
        self.assertEqual(
            calls,
            [
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--no-color-output",
                    "--no-error-summary",
                    "--follow-imports=silent",
                    path,
                ]
                for path in ("a/first.py", "z/last.py")
            ],
        )

        with patch.object(a2_check, "run", return_value=17) as run_gate:
            self.assertEqual(a2_check.main(["--files", "a/first.py"]), 17)
        run_gate.assert_called_once_with(["a/first.py"], mypy_workers=1)

        with patch.object(a2_check, "run", return_value=23) as run_gate:
            self.assertEqual(
                a2_check.main(["--mypy-workers", "4", "--files", "a/first.py"]),
                23,
            )
        run_gate.assert_called_once_with(["a/first.py"], mypy_workers=4)

    def test_parallel_calls_have_disjoint_cache_roots_and_cleanup(self) -> None:
        targets = ["a/first.py", "b/second.py", "c/third.py", "d/fourth.py"]
        invocation_caches: list[set[str]] = [set(), set()]
        current_invocation = 0

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            cache_index = args.index("--cache-dir")
            cache_dir = Path(args[cache_index + 1])
            cache_dir.mkdir(parents=True, exist_ok=True)
            invocation_caches[current_invocation].add(str(cache_dir))
            return 0, "", ""

        with patch.object(a2_check, "_run_streams", side_effect=fake_run):
            for invocation in range(2):
                current_invocation = invocation
                passed, results = a2_check._run_mypy_targets(targets, workers=2)
                self.assertTrue(passed)
                self.assertEqual(len(results), len(targets))

        self.assertEqual(len(invocation_caches[0]), 2)
        self.assertEqual(len(invocation_caches[1]), 2)
        self.assertTrue(invocation_caches[0].isdisjoint(invocation_caches[1]))
        self.assertTrue(all(not Path(cache).exists() for cache in set.union(*invocation_caches)))

    def test_worker_schedule_exception_is_recorded_for_every_target(self) -> None:
        targets = ["a/first.py", "b/second.py", "c/third.py", "d/fourth.py"]
        with patch.object(
            a2_check.ThreadPoolExecutor,
            "submit",
            side_effect=RuntimeError("mock scheduling failure"),
        ):
            passed, results = a2_check._run_mypy_targets(targets, workers=2)

        self.assertFalse(passed)
        self.assertEqual([result.path for result in results], sorted(targets))
        self.assertEqual([result.returncode for result in results], [127, 127, 127, 127])
        self.assertTrue(
            all(
                result.stderr == "mypy worker scheduling failed (RuntimeError)\n"
                for result in results
            )
        )

    def test_executor_cleanup_exception_fails_aggregate_and_preserves_target_streams(self) -> None:
        targets = ["b/second.py", "a/first.py"]
        original_exit = a2_check.ThreadPoolExecutor.__exit__

        def exit_then_raise(executor, exc_type, exc_value, traceback):
            original_exit(executor, exc_type, exc_value, traceback)
            raise RuntimeError("private executor cleanup detail")

        captured = io.StringIO()
        with (
            patch.object(
                a2_check,
                "_run_streams",
                side_effect=lambda args: (0, f"out:{args[-1]}", f"err:{args[-1]}"),
            ),
            patch.object(a2_check.ThreadPoolExecutor, "__exit__", new=exit_then_raise),
            contextlib.redirect_stderr(captured),
        ):
            passed, results = a2_check._run_mypy_targets(targets, workers=2)

        self.assertFalse(passed)
        self.assertEqual([result.path for result in results], ["a/first.py", "b/second.py"])
        self.assertEqual([result.returncode for result in results], [0, 0])
        self.assertEqual(
            [(result.stdout, result.stderr) for result in results],
            [("out:a/first.py", "err:a/first.py"), ("out:b/second.py", "err:b/second.py")],
        )
        self.assertEqual(captured.getvalue(), "mypy worker pool lifecycle failed (RuntimeError)\n")
        self.assertNotIn("private executor cleanup detail", captured.getvalue())

    def test_temporary_directory_cleanup_exception_fails_aggregate_without_target_rewrite(
        self,
    ) -> None:
        class CleanupFailingTemporaryDirectory(tempfile.TemporaryDirectory):
            def __exit__(self, exc_type, exc_value, traceback):
                super().__exit__(exc_type, exc_value, traceback)
                raise OSError("private temporary cleanup detail")

        targets = ["a/first.py", "b/second.py"]
        captured = io.StringIO()
        with (
            patch.object(
                a2_check,
                "_run_streams",
                side_effect=lambda args: (0, f"out:{args[-1]}", f"err:{args[-1]}"),
            ),
            patch.object(
                a2_check.tempfile,
                "TemporaryDirectory",
                CleanupFailingTemporaryDirectory,
            ),
            contextlib.redirect_stderr(captured),
        ):
            passed, results = a2_check._run_mypy_targets(targets, workers=2)

        self.assertFalse(passed)
        self.assertEqual([result.path for result in results], targets)
        self.assertEqual([result.returncode for result in results], [0, 0])
        self.assertEqual(
            [(result.stdout, result.stderr) for result in results],
            [("out:a/first.py", "err:a/first.py"), ("out:b/second.py", "err:b/second.py")],
        )
        self.assertEqual(
            captured.getvalue(),
            "mypy temporary cache lifecycle failed (OSError)\n",
        )
        self.assertNotIn("private temporary cleanup detail", captured.getvalue())

    def test_worker_count_validation_and_balanced_chunking(self) -> None:
        self.assertEqual(
            a2_check._mypy_target_chunks(["a", "b", "c", "d", "e", "f"], 4),
            [
                (0, ("a", "b")),
                (1, ("c", "d")),
                (2, ("e",)),
                (3, ("f",)),
            ],
        )
        self.assertEqual(a2_check._mypy_target_chunks([], 4), [])
        for invalid in (0, -1, True):
            with self.assertRaises(ValueError):
                a2_check._mypy_target_chunks(["a.py"], invalid)


if __name__ == "__main__":
    unittest.main()
