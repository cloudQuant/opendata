"""Pure stdlib checks for the AC21 policy probe; no pytest or OS probe."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

import ac21_sandbox_policy_probe as probe


class FakeSocket:
    def __init__(self, family: int, assigned_ephemeral_port: int) -> None:
        self.family = family
        self.assigned_ephemeral_port = assigned_ephemeral_port
        self.bound_address: tuple[str, int] | None = None
        self.closed = False
        self.options: list[tuple[int, int, int]] = []
        self.listening = False

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        host, requested_port = address
        self.bound_address = (
            host,
            self.assigned_ephemeral_port if requested_port == 0 else requested_port,
        )

    def listen(self, backlog: int = 128) -> None:
        self.listening = True

    def getsockname(self) -> tuple[object, ...]:
        if self.bound_address is None:
            raise OSError("fake socket has not been bound")
        return self.bound_address

    def fileno(self) -> int:
        return 0

    def setblocking(self, flag: bool) -> None:
        return None

    def accept(self) -> tuple[probe.SocketLike, object]:
        raise BlockingIOError

    def close(self) -> None:
        self.closed = True


class FakeSocketFactory:
    def __init__(self, *, fail_ipv6: bool = False) -> None:
        self.created: list[FakeSocket] = []
        self.fail_ipv6 = fail_ipv6

    def __call__(self, family: int, kind: int) -> FakeSocket:
        if self.fail_ipv6 and family == socket.AF_INET6:
            raise OSError("injected IPv6 unavailable")
        assigned_port = 41000 if not self.created else 41001
        fake_socket = FakeSocket(family, assigned_port)
        self.created.append(fake_socket)
        return fake_socket


class EndpointAndProfileTests(unittest.TestCase):
    def test_endpoint_set_is_fixed_loopback_and_uses_both_controls(self) -> None:
        endpoints = probe._endpoint_specs(42000, 42001)
        self.assertEqual(
            [(item.name, item.host, item.port) for item in endpoints],
            [
                ("approved_ipv4", "127.0.0.1", 42000),
                ("wrong_port_ipv4", "127.0.0.1", 42001),
                ("ipv6_alias_same_port", "::1", 42000),
                ("ipv4_alias_same_port", "127.0.0.2", 42000),
            ],
        )
        self.assertEqual({item.family for item in endpoints}, {socket.AF_INET, socket.AF_INET6})

    def test_profile_is_default_deny_with_one_localhost_port_allow(self) -> None:
        profile = probe.render_profile(42000)
        self.assertEqual(
            profile,
            "(version 1) (deny default) (allow process-fork) (allow process-exec) "
            "(allow file-read*) "
            '(allow network-outbound (remote ip "localhost:42000"))',
        )
        self.assertEqual(profile.count("process-fork"), 1)
        self.assertEqual(profile.count("process-exec"), 1)
        self.assertEqual(profile.count("network-outbound"), 1)
        self.assertNotIn("network-inbound", profile)
        self.assertNotIn("127.0.0.1", profile)
        self.assertNotIn("::1", profile)
        self.assertNotIn("127.0.0.2", profile)

    def test_ports_reject_missing_invalid_boolean_wildcard_and_duplicate(self) -> None:
        invalid_ports: tuple[object, ...] = (None, True, False, 0, -1, 65536, "42000", "*")
        for invalid in invalid_ports:
            with self.subTest(port=invalid), self.assertRaises(ValueError):
                probe._validate_port(invalid, "approved_port")
        with self.assertRaises(ValueError):
            probe._endpoint_specs(42000, 42000)
        with self.assertRaises(ValueError):
            probe._endpoint_specs(0, 42001)

    def test_owned_listener_setup_uses_only_injected_sockets(self) -> None:
        factory = FakeSocketFactory()
        result = probe.open_owned_listeners(factory)
        self.assertEqual(result.approved_port, 41000)
        self.assertEqual(result.wrong_port, 41001)
        self.assertEqual(
            [item.endpoint.name for item in result.listeners], list(probe.ENDPOINT_NAMES)
        )
        self.assertEqual(
            [item.endpoint.host for item in result.listeners],
            ["127.0.0.1", "127.0.0.1", "::1", "127.0.0.2"],
        )
        self.assertEqual(len(factory.created), 4)
        self.assertTrue(all(item.listening for item in factory.created))
        self.assertIn((socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1), factory.created[2].options)
        cleanup = probe._close_owned_listeners(result.listeners)
        self.assertEqual(cleanup["closed_listener_names"], list(probe.ENDPOINT_NAMES))
        self.assertTrue(all(item.closed for item in factory.created))

    def test_unavailable_alias_binding_closes_only_partial_injected_sockets(self) -> None:
        factory = FakeSocketFactory(fail_ipv6=True)
        with self.assertRaises(probe.ListenerUnavailableError) as raised:
            probe.open_owned_listeners(factory)
        self.assertEqual(len(factory.created), 2)
        self.assertTrue(all(item.closed for item in factory.created))
        self.assertEqual(
            raised.exception.cleanup["partial_listener_names"],
            ["approved_ipv4", "ipv4_alias_same_port"],
        )


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zero_counts = dict.fromkeys(probe.ENDPOINT_NAMES, 0)

    def test_measured_positive_and_three_denials_are_required_for_pass(self) -> None:
        successes = self.zero_counts.copy()
        accepts = self.zero_counts.copy()
        successes["approved_ipv4"] = 2
        accepts["approved_ipv4"] = 2
        result = probe.classify_observations(successes, accepts)
        self.assertEqual(result.status, probe.STATUS_PASS)

    def test_each_negative_connect_is_unsatisfied_exact_host(self) -> None:
        for name in probe.NEGATIVE_ENDPOINT_NAMES:
            successes = self.zero_counts.copy()
            accepts = self.zero_counts.copy()
            successes["approved_ipv4"] = 2
            accepts["approved_ipv4"] = 2
            successes[name] = 1
            with self.subTest(endpoint=name):
                self.assertEqual(
                    probe.classify_observations(successes, accepts).status,
                    probe.STATUS_UNSATISFIED,
                )

    def test_negative_accept_without_worker_success_is_still_unsatisfied(self) -> None:
        successes = self.zero_counts.copy()
        accepts = self.zero_counts.copy()
        accepts["ipv4_alias_same_port"] = 1
        self.assertEqual(
            probe.classify_observations(successes, accepts).status,
            probe.STATUS_UNSATISFIED,
        )

    def test_unavailable_is_not_run_and_missing_positive_is_probe_failure(self) -> None:
        self.assertEqual(
            probe.classify_observations(
                self.zero_counts, self.zero_counts, unavailable=True
            ).status,
            probe.STATUS_UNAVAILABLE,
        )
        self.assertEqual(
            probe.classify_observations(self.zero_counts, self.zero_counts).status,
            probe.STATUS_FAILED,
        )

    def test_counter_maps_require_exact_names_and_nonnegative_integers(self) -> None:
        extra = self.zero_counts | {"external_target": 0}
        with self.assertRaises(ValueError):
            probe.classify_observations(extra, self.zero_counts)
        invalid = self.zero_counts | {"approved_ipv4": True}
        with self.assertRaises(ValueError):
            probe.classify_observations(invalid, self.zero_counts)
        invalid_negative = self.zero_counts | {"approved_ipv4": -1}
        with self.assertRaises(ValueError):
            probe.classify_observations(invalid_negative, self.zero_counts)

    def test_cleanup_gate_requires_every_owned_listener_closed(self) -> None:
        pass_result = probe.Classification(probe.STATUS_PASS, "counterfactuals denied")
        complete = {
            "closed_listener_names": list(probe.ENDPOINT_NAMES),
            "close_errors": {},
        }
        self.assertEqual(
            probe.enforce_cleanup_gate(
                pass_result,
                expected_listener_names=probe.ENDPOINT_NAMES,
                cleanup=complete,
                process_group_kill_error=None,
            ),
            pass_result,
        )
        close_error = complete | {"close_errors": {"approved_ipv4": "OSError"}}
        self.assertEqual(
            probe.enforce_cleanup_gate(
                pass_result,
                expected_listener_names=probe.ENDPOINT_NAMES,
                cleanup=close_error,
                process_group_kill_error=None,
            ).status,
            probe.STATUS_FAILED,
        )
        missing_close = complete | {"closed_listener_names": list(probe.ENDPOINT_NAMES[:-1])}
        self.assertEqual(
            probe.enforce_cleanup_gate(
                pass_result,
                expected_listener_names=probe.ENDPOINT_NAMES,
                cleanup=missing_close,
                process_group_kill_error=None,
            ).status,
            probe.STATUS_FAILED,
        )

    def test_cleanup_gate_rejects_owned_process_group_cleanup_error(self) -> None:
        pass_result = probe.Classification(probe.STATUS_PASS, "counterfactuals denied")
        complete = {
            "closed_listener_names": list(probe.ENDPOINT_NAMES),
            "close_errors": {},
        }
        result = probe.enforce_cleanup_gate(
            pass_result,
            expected_listener_names=probe.ENDPOINT_NAMES,
            cleanup=complete,
            process_group_kill_error="PermissionError",
        )
        self.assertEqual(result.status, probe.STATUS_FAILED)
        self.assertIn("PermissionError", result.reason)


class DryRunAndIsolationTests(unittest.TestCase):
    def test_default_cli_is_dry_and_never_calls_socket_or_process_creation(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(
                probe, "open_owned_listeners", side_effect=AssertionError("socket setup called")
            ),
            patch.object(probe.subprocess, "Popen", side_effect=AssertionError("process started")),
            patch.object(probe.subprocess, "run", side_effect=AssertionError("process started")),
            contextlib.redirect_stdout(stdout),
        ):
            result = probe.main([])
        self.assertEqual(result, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], probe.STATUS_DRY_RUN)
        self.assertEqual(report["effects"]["socket_created"], False)
        self.assertEqual(report["effects"]["process_spawned"], False)

    def test_public_cli_rejects_arbitrary_commands_targets_and_internal_worker_flags(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            probe.main(["/bin/sh", "-c", "true"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            probe.main(["--_ac21-owned-worker", "child", "42000", "42001"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_internal_worker_command_is_fixed_and_uses_python_bootstrap(self) -> None:
        command = probe._worker_argv("child", 42000, 42001)
        self.assertEqual(command[:3], [probe.sys.executable, "-S", "-c"])
        self.assertEqual(command[3], probe.WORKER_BOOTSTRAP)
        self.assertEqual(command[5:], ["child", "42000", "42001"])
        self.assertEqual(command[4], str(Path(probe.__file__).resolve()))
        with self.assertRaises(ValueError):
            probe._worker_argv("other", 42000, 42001)

    def test_probe_source_has_no_product_or_pytest_imports(self) -> None:
        source = Path(probe.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertFalse(imported_roots & {"opendata", "pytest", "sqlalchemy"})
        self.assertNotIn("shell=True", source)
        self.assertNotIn("urlopen(", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
