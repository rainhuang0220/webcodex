"""Deterministic checks for the Debian/Ubuntu .deb lifecycle harness.

These tests drive a fake command runner. They do not install a package, start
systemd, or accept any native matrix row.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.deb_lifecycle_acceptance import (
    AcceptanceConfig,
    AcceptanceError,
    CommandResult,
    SubprocessRunner,
    acceptance,
    candidate_flags,
    container_signals,
    main,
)


def ok(stdout: str = "", exit_code: int = 0) -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr="")


def deb_control(package: str = "webcodex-runtime", version: str = "0.5.1", arch: str = "amd64") -> str:
    return f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n"


class MapView:
    def __init__(self, os_release: str, arch_files: dict[str, bool] | None = None, container: bool = False):
        self.os_release = os_release
        self.present = set(arch_files or [])
        self.container = container

    def read_text(self, path: str) -> str | None:
        if path == "/etc/os-release":
            return self.os_release
        return None

    def executable(self, path: str) -> bool:
        return path in self.present

    def containerized(self) -> bool:
        return self.container


class ScriptedRunner:
    def __init__(self, respond):
        self.respond = respond
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout: float) -> CommandResult:
        self.calls.append(list(argv))
        outcome = self.respond(list(argv), timeout)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


DEBIAN_12 = "ID=debian\nVERSION_ID=12\n"
RUNTIME_PATHS = [
    "/usr/bin/webcodex",
    "/usr/bin/webcodex-server",
    "/usr/bin/webcodex-runner",
    "/usr/lib/webcodex/webcodex-runtime/webcodex",
    "/usr/lib/webcodex/webcodex-runtime/webcodex-server",
    "/usr/lib/webcodex/webcodex-runtime/webcodex-runner",
]


def installed_view() -> MapView:
    return MapView(DEBIAN_12, {path: True for path in RUNTIME_PATHS})


def config(root: Path, **overrides) -> AcceptanceConfig:
    deb = root / "webcodex-runtime.deb"
    deb.write_bytes(b"fake-deb")
    values = dict(
        deb=deb,
        previous_deb=None,
        execute=False,
        disposable_environment=None,
        exercise_existing_units=False,
        uninstall_only=False,
        package=None,
        timeout_seconds=5,
    )
    values.update(overrides)
    return AcceptanceConfig(**values)


class DebLifecycleAcceptanceTests(unittest.TestCase):
    def test_plan_hashes_the_artifact_and_does_not_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            planned = config(root)
            runner = ScriptedRunner(lambda argv, timeout: ok(deb_control()))

            report, code = acceptance(planned, runner, installed_view())

        self.assertEqual(code, 0)
        self.assertFalse(report["executed"])
        self.assertEqual(report["matrix_updated"], False)
        self.assertIsNone(report["candidate_target"])
        self.assertFalse(any(argv[:2] == ["dpkg", "-i"] for argv in runner.calls))
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["artifact"]["status"], "PASS")
        self.assertEqual(steps["artifact"]["evidence"]["sha256"], hashlib.sha256(b"fake-deb").hexdigest())
        self.assertEqual(steps["install"]["status"], "NOT_RUN")
        self.assertEqual(steps["upgrade"]["status"], "UNTESTED")
        self.assertEqual(steps["reboot"]["status"], "UNTESTED")
        self.assertNotEqual(steps["upgrade"]["status"], "PASS")
        self.assertEqual(report["upgrade_acceptance_candidate"], False)
        self.assertEqual(report["systemd_acceptance_candidate"], False)
        self.assertEqual(report["installer_acceptance_candidate"], False)

    def test_execute_without_disposable_authorization_refuses_before_dpkg(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = ScriptedRunner(lambda argv, timeout: ok(deb_control()))
            report, code = acceptance(
                config(root, execute=True),
                runner,
                installed_view(),
            )

        self.assertEqual(code, 3)
        self.assertFalse(report["executed"])
        self.assertIn("disposable", report["refused"])
        self.assertFalse(any(argv[:2] == ["dpkg", "-i"] for argv in runner.calls))

    def test_fresh_package_lifecycle_leaves_upgrade_and_systemd_unclaimed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view = installed_view()

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[:2] == ["dpkg", "-i"]:
                    return ok("")
                if argv[0] == "systemctl":
                    return ok("")
                if argv[:2] == ["dpkg", "--purge"]:
                    view.present.clear()
                    return ok("")
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(exit_code=1, stdout="", stderr="no package")
                return ok("")

            runner = ScriptedRunner(respond)
            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                runner,
                view,
            )

        self.assertEqual(code, 0, json.dumps(report, indent=2))
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["install"]["status"], "PASS")
        self.assertEqual(steps["install"]["exit_code"], 0)
        self.assertEqual(steps["binaries"]["status"], "PASS")
        self.assertEqual(steps["upgrade"]["status"], "UNTESTED")
        self.assertEqual(steps["services_after_install"]["status"], "PASS")
        self.assertEqual(steps["systemd_start"]["status"], "NOT_APPLICABLE")
        self.assertEqual(steps["health"]["status"], "NOT_APPLICABLE")
        self.assertEqual(steps["restart"]["status"], "NOT_APPLICABLE")
        self.assertEqual(steps["uninstall"]["status"], "PASS")
        self.assertEqual(steps["residue"]["status"], "PASS")
        self.assertEqual(steps["reboot"]["status"], "UNTESTED")
        self.assertEqual(report["host"]["id"], "debian")
        self.assertEqual(report["host"]["version_id"], "12")
        self.assertEqual(report["host"]["dpkg_arch"], "amd64")
        self.assertEqual(report["installer_acceptance_candidate"], False)
        self.assertEqual(report["systemd_acceptance_candidate"], False)
        self.assertIsNone(report["candidate_target"])
        self.assertEqual(runner.calls[0][:2], ["dpkg-deb", "-f"])
        self.assertIn(["dpkg", "-i", str(root / "webcodex-runtime.deb")], runner.calls)
        self.assertIn(["dpkg", "--purge", "webcodex-runtime"], runner.calls)

    def test_install_failure_preserves_exit_code_and_skips_later_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[0] == "systemctl":
                    return ok("")
                if argv[:2] == ["dpkg", "-i"]:
                    return CommandResult(exit_code=2, stdout="", stderr="dependency problem")
                raise AssertionError(argv)

            runner = ScriptedRunner(respond)
            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                runner,
                installed_view(),
            )

        self.assertEqual(code, 2)
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["install"]["status"], "FAIL")
        self.assertEqual(steps["install"]["exit_code"], 2)
        self.assertIn("dependency", steps["install"]["reason"])
        self.assertEqual(steps["artifact"]["evidence"]["version"], "0.5.1")
        self.assertEqual(steps["artifact"]["evidence"]["architecture"], "amd64")
        self.assertEqual(steps["binaries"]["status"], "NOT_RUN")
        self.assertEqual(steps["uninstall"]["status"], "NOT_RUN")
        self.assertFalse(any(argv[:2] == ["dpkg", "--purge"] for argv in runner.calls))

    def test_timeout_is_a_failure_without_an_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[0] == "systemctl":
                    return ok("")
                if argv[:2] == ["dpkg", "-i"]:
                    return CommandResult(exit_code=None, stdout="", stderr="still running", timed_out=True)
                raise AssertionError(argv)

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                ScriptedRunner(respond),
                installed_view(),
            )

        self.assertEqual(code, 2)
        install = next(step for step in report["steps"] if step["id"] == "install")
        self.assertEqual(install["status"], "FAIL")
        self.assertIsNone(install["exit_code"])
        self.assertIn("timed out", install["reason"])

    def test_interrupt_during_purge_keeps_the_partial_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg", "--purge"]:
                    raise KeyboardInterrupt()
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[0] == "systemctl":
                    return ok("")
                return ok("")

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                ScriptedRunner(respond),
                installed_view(),
            )

        self.assertEqual(code, 130)
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["install"]["status"], "PASS")
        self.assertEqual(steps["uninstall"]["status"], "INTERRUPTED")
        self.assertEqual(steps["residue"]["status"], "NOT_RUN")

    def test_container_arm64_success_does_not_claim_native_x64_systemd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view = MapView(DEBIAN_12, {path: True for path in RUNTIME_PATHS}, container=True)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control(arch="arm64"))
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("arm64\n")
                if argv[:3] == ["systemctl", "list-unit-files", "webcodex*.service"]:
                    return ok("webcodex-server.service disabled\nwebcodex-runner.service disabled\n")
                if argv[:3] == ["systemctl", "--user", "list-unit-files"]:
                    return CommandResult(exit_code=1, stdout="", stderr="Failed to connect to bus")
                if "is-active" in argv:
                    return ok("active\n")
                if argv[:2] == ["dpkg", "--purge"]:
                    view.present.clear()
                    return ok("")
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(exit_code=1, stdout="", stderr="")
                return ok("")

            report, code = acceptance(
                config(
                    root,
                    execute=True,
                    disposable_environment="docker-arm64",
                    exercise_existing_units=True,
                ),
                ScriptedRunner(respond),
                view,
            )

        self.assertEqual(code, 0, json.dumps(report, indent=2))
        self.assertEqual(report["host"]["dpkg_arch"], "arm64")
        self.assertTrue(report["host"]["containerized"])
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["systemd_start"]["status"], "PASS")
        self.assertEqual(steps["restart"]["status"], "PASS")
        self.assertEqual(report["systemd_acceptance_candidate"], False)
        self.assertEqual(report["installer_acceptance_candidate"], False)
        self.assertIsNone(report["candidate_target"])
        self.assertNotIn("x64", json.dumps(report["candidate_target"]))

    def test_previous_package_is_installed_before_the_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = root / "previous.deb"
            previous.write_bytes(b"old-deb")
            seen: list[str] = []

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    version = "0.5.0" if argv[2].endswith("previous.deb") else "0.5.1"
                    return ok(deb_control(version=version))
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[:2] == ["dpkg", "-i"]:
                    seen.append(Path(argv[2]).name)
                    return ok("")
                if argv[0] == "systemctl":
                    return ok("")
                if argv[:2] == ["dpkg", "--purge"]:
                    view.present.clear()
                    return ok("")
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(exit_code=1, stdout="", stderr="")
                return ok("")

            view = installed_view()
            report, code = acceptance(
                config(
                    root,
                    previous_deb=previous,
                    execute=True,
                    disposable_environment="vm-debian-12",
                ),
                ScriptedRunner(respond),
                view,
            )

        self.assertEqual(code, 0, json.dumps(report, indent=2))
        self.assertEqual(seen, ["previous.deb", "webcodex-runtime.deb"])
        upgrade = next(step for step in report["steps"] if step["id"] == "upgrade")
        self.assertEqual(upgrade["status"], "PASS")
        self.assertEqual(upgrade["exit_code"], 0)
        self.assertEqual(report["upgrade_acceptance_candidate"], False)

    def test_architecture_mismatch_does_not_call_dpkg_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control(arch="arm64"))
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[0] == "systemctl":
                    return ok("")
                raise AssertionError(argv)

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                ScriptedRunner(respond),
                installed_view(),
            )

        self.assertEqual(code, 2)
        install = next(step for step in report["steps"] if step["id"] == "install")
        self.assertEqual(install["status"], "FAIL")
        self.assertIn("architecture", install["reason"])

    def test_preexisting_units_abort_before_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[:2] == ["systemctl", "list-unit-files"]:
                    return ok("webcodex-server.service enabled\n")
                if argv[:3] == ["systemctl", "--user", "list-unit-files"]:
                    return ok("")
                raise AssertionError(argv)

            runner = ScriptedRunner(respond)
            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                runner,
                installed_view(),
            )

        self.assertEqual(code, 2)
        step = next(item for item in report["steps"] if item["id"] == "preexisting_units")
        self.assertEqual(step["status"], "FAIL")
        self.assertFalse(any(argv[:2] == ["dpkg", "-i"] for argv in runner.calls))

    def test_uninstall_only_is_recovery_and_upgrade_stays_untested(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view = installed_view()

            def respond(argv, timeout):
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[0] == "systemctl":
                    return ok("")
                if argv[:2] == ["dpkg", "--purge"]:
                    view.present.clear()
                    return ok("")
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(exit_code=1, stdout="", stderr="")
                raise AssertionError(argv)

            report, code = acceptance(
                AcceptanceConfig(
                    deb=None,
                    previous_deb=None,
                    execute=True,
                    disposable_environment="vm-debian-12",
                    exercise_existing_units=False,
                    uninstall_only=True,
                    package="webcodex-runtime",
                    timeout_seconds=5,
                ),
                ScriptedRunner(respond),
                view,
            )

        self.assertEqual(code, 0, json.dumps(report, indent=2))
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["upgrade"]["status"], "UNTESTED")
        self.assertEqual(steps["uninstall"]["status"], "PASS")
        self.assertEqual(steps["residue"]["status"], "PASS")
        self.assertEqual(steps["install"]["status"], "NOT_RUN")

    def test_host_predicate_never_promotes_untested_upgrade_or_container_systemd(self):
        host = {"id": "debian", "version_id": "12", "dpkg_arch": "amd64", "containerized": False}
        untested = candidate_flags(
            host,
            {"install": "PASS", "binaries": "PASS", "uninstall": "PASS", "residue": "PASS",
             "upgrade": "UNTESTED", "systemd_start": "NOT_APPLICABLE", "health": "NOT_APPLICABLE",
             "restart": "NOT_APPLICABLE"},
            host_execution=True,
        )
        self.assertTrue(untested["installer_acceptance_candidate"])
        self.assertFalse(untested["upgrade_acceptance_candidate"])
        self.assertFalse(untested["systemd_acceptance_candidate"])
        self.assertEqual(untested["candidate_target"], "Debian 12 x64, .deb")
        self.assertFalse(untested["matrix_updated"])

        container = candidate_flags(
            {"id": "ubuntu", "version_id": "24.04", "dpkg_arch": "arm64", "containerized": True},
            {"install": "PASS", "binaries": "PASS", "uninstall": "PASS", "residue": "PASS",
             "upgrade": "PASS", "systemd_start": "PASS", "health": "PASS", "restart": "PASS"},
            host_execution=True,
        )
        self.assertFalse(container["systemd_acceptance_candidate"])
        self.assertFalse(container["installer_acceptance_candidate"])
        self.assertIsNone(container["candidate_target"])

        scripted = candidate_flags(host, {"install": "PASS", "binaries": "PASS", "uninstall": "PASS", "residue": "PASS", "upgrade": "PASS", "systemd_start": "PASS", "health": "PASS", "restart": "PASS"}, host_execution=False)
        self.assertFalse(scripted["installer_acceptance_candidate"])
        self.assertFalse(scripted["systemd_acceptance_candidate"])

    def test_subprocess_runner_refuses_package_installation(self):
        runner = SubprocessRunner(allow_mutation=False)
        with self.assertRaises(AcceptanceError):
            runner.run(["dpkg", "-i", "webcodex.deb"], timeout=1)

    def test_equal_version_is_not_an_upgrade_and_does_not_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = root / "previous.deb"
            previous.write_bytes(b"old-deb")

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control(version="0.5.1"))
                raise AssertionError(argv)

            report, code = acceptance(
                config(root, previous_deb=previous, execute=True, disposable_environment="vm-debian-12"),
                ScriptedRunner(respond),
                installed_view(),
            )

        self.assertEqual(code, 2)
        upgrade = next(step for step in report["steps"] if step["id"] == "upgrade")
        self.assertEqual(upgrade["status"], "FAIL")
        self.assertIn("not newer", upgrade["reason"])

    def test_architecture_all_does_not_match_a_native_host(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control(arch="all"))
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[0] == "systemctl":
                    return ok("")
                raise AssertionError(argv)

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                ScriptedRunner(respond),
                installed_view(),
            )

        self.assertEqual(code, 2)
        install = next(step for step in report["steps"] if step["id"] == "install")
        self.assertEqual(install["status"], "FAIL")
        self.assertIn("architecture", install["reason"])

    def test_uncertain_package_query_does_not_pass_residue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view = installed_view()

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[:2] == ["dpkg", "--purge"]:
                    view.present.clear()
                    return ok("")
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(exit_code=2, stdout="", stderr="database locked")
                return ok("")

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                ScriptedRunner(respond),
                view,
            )

        self.assertEqual(code, 2)
        residue = next(step for step in report["steps"] if step["id"] == "residue")
        self.assertEqual(residue["status"], "FAIL")
        self.assertEqual(residue["exit_code"], 2)
        self.assertNotEqual(residue["status"], "PASS")

    def test_non_executable_leftover_fails_residue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view = installed_view()
            view.leftovers: set[str] = set()
            view.exists = lambda path: path in view.present or path in view.leftovers

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[:2] == ["dpkg", "--purge"]:
                    view.leftovers.add(RUNTIME_PATHS[0])
                    view.present.clear()
                    return ok("")
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(exit_code=1, stdout="", stderr="")
                return ok("")

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12"),
                ScriptedRunner(respond),
                view,
            )

        self.assertEqual(code, 2, json.dumps(report, indent=2))
        residue = next(step for step in report["steps"] if step["id"] == "residue")
        self.assertEqual(residue["status"], "FAIL")
        self.assertIn("path remains", residue["reason"])
        self.assertIn(RUNTIME_PATHS[0], residue["evidence"]["binaries"])

    def test_inactive_unit_keeps_the_start_result_and_does_not_purge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[:3] == ["systemctl", "list-unit-files", "webcodex*.service"]:
                    return ok("webcodex-server.service disabled\n")
                if argv[:3] == ["systemctl", "--user", "list-unit-files"]:
                    return ok("")
                if argv[:2] == ["dpkg", "-i"]:
                    return ok("")
                if "is-active" in argv:
                    return ok("inactive\n")
                if "start" in argv or "restart" in argv or "stop" in argv:
                    return ok("")
                raise AssertionError(argv)

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12", exercise_existing_units=True),
                ScriptedRunner(respond),
                installed_view(),
            )

        self.assertEqual(code, 2, json.dumps(report, indent=2))
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["systemd_start"]["status"], "PASS")
        self.assertEqual(steps["health"]["status"], "FAIL")
        self.assertIn("inactive", steps["health"]["reason"])
        self.assertEqual(steps["restart"]["status"], "NOT_RUN")
        self.assertEqual(steps["uninstall"]["status"], "NOT_RUN")

    def test_health_timeout_still_records_the_successful_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def respond(argv, timeout):
                if argv[:2] == ["dpkg-deb", "-f"]:
                    return ok(deb_control())
                if argv[:2] == ["dpkg", "--print-architecture"]:
                    return ok("amd64\n")
                if argv[:3] == ["systemctl", "list-unit-files", "webcodex*.service"]:
                    return ok("webcodex-server.service disabled\n")
                if argv[:3] == ["systemctl", "--user", "list-unit-files"]:
                    return ok("")
                if argv[:2] == ["dpkg", "-i"]:
                    return ok("")
                if "is-active" in argv:
                    return CommandResult(exit_code=None, stdout="", stderr="still running", timed_out=True)
                if "start" in argv or "stop" in argv:
                    return ok("")
                raise AssertionError(argv)

            report, code = acceptance(
                config(root, execute=True, disposable_environment="vm-debian-12", exercise_existing_units=True),
                ScriptedRunner(respond),
                installed_view(),
            )

        self.assertEqual(code, 2, json.dumps(report, indent=2))
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(steps["systemd_start"]["status"], "PASS")
        self.assertEqual(steps["health"]["status"], "FAIL")
        self.assertIsNone(steps["health"]["exit_code"])
        self.assertIn("timed out", steps["health"]["reason"])
        self.assertEqual(steps["uninstall"]["status"], "NOT_RUN")

    def test_container_signals_recognize_docker_markers_and_cgroup(self):
        quiet = lambda path: None
        absent = lambda path: False
        self.assertFalse(container_signals(quiet, {}, absent))
        self.assertTrue(container_signals(quiet, {}, lambda path: path == "/.dockerenv"))
        self.assertTrue(container_signals(lambda path: "docker\n" if path == "/run/systemd/container" else None, {}, absent))
        self.assertTrue(container_signals(lambda path: "0::/docker/abc\n" if path == "/proc/1/cgroup" else None, {}, absent))
        self.assertFalse(container_signals(lambda path: "0::/init.scope\n" if path == "/proc/1/cgroup" else None, {}, absent))

    def test_subprocess_timeout_kills_the_group_and_closes_stdin(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "pid"
            script = (
                "import subprocess, time\n"
                "child = subprocess.Popen(['sleep', '30'])\n"
                f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(30)\n"
            )
            runner = SubprocessRunner(allow_mutation=False)
            result = runner.run([sys.executable, "-c", script], timeout=1)
            self.assertTrue(result.timed_out)
            self.assertIsNone(result.exit_code)
            deadline = time.time() + 3
            pid = None
            while time.time() < deadline and not pidfile.exists():
                time.sleep(0.05)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text().strip())
            gone = False
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    gone = True
                    break
                time.sleep(0.05)
            if not gone:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            self.assertTrue(gone)
            echo = runner.run(
                [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read(1) or 'eof')"],
                timeout=2,
            )
        self.assertEqual(echo.exit_code, 0)
        self.assertEqual(echo.stdout, "eof")

    def test_subprocess_runner_drops_ambient_tokens(self):
        key = "WEBCODEX_TEST_TOKEN"
        os.environ[key] = "not-a-real-token"
        try:
            result = SubprocessRunner(allow_mutation=False).run(
                [sys.executable, "-c", f"import os,sys; sys.stdout.write(os.environ.get({key!r}, 'absent'))"],
                timeout=2,
            )
        finally:
            os.environ.pop(key, None)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "absent")

    def test_timeout_must_be_finite(self):
        self.assertEqual(main(["--timeout", "nan", "missing.deb"]), 1)


if __name__ == "__main__":
    unittest.main()
