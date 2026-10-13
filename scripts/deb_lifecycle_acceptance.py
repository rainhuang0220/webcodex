#!/usr/bin/env python3
"""Check one Debian/Ubuntu .deb lifecycle without becoming a second installer.

The default run hashes the artifact and prints a plan. Package validity is a
separate result: it passes only when dpkg-deb returns usable control, and it
stays unverified when dpkg-deb is missing. The plan does not install, purge,
or change services. --execute also requires --disposable-environment LABEL
and is only for a Linux host the operator can destroy.

scripts/package_unified_installer.py remains the package builder. This
checklist never edits the native acceptance matrix. A fake command runner is
not native systemd acceptance. Upgrade is UNTESTED unless --previous-deb is
supplied. Reboot persistence is always UNTESTED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_PACKAGES = ("webcodex", "webcodex-runtime")
CONFLICT_PACKAGES = {"webcodex": "webcodex-runtime", "webcodex-runtime": "webcodex"}
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
UNIT_RE = re.compile(r"^(webcodex[A-Za-z0-9@._-]*\.service)\s+(\S+)")
DPKG_MUTATIONS = {"-i", "--install", "--purge", "-P", "-r", "--remove"}
SYSTEMCTL_MUTATIONS = {"start", "restart", "enable", "disable", "stop"}
RUNTIME_PATHS = (
    "/usr/bin/webcodex",
    "/usr/bin/webcodex-server",
    "/usr/bin/webcodex-runner",
    "/usr/lib/webcodex/webcodex-runtime/webcodex",
    "/usr/lib/webcodex/webcodex-runtime/webcodex-server",
    "/usr/lib/webcodex/webcodex-runtime/webcodex-runner",
)
DESKTOP_PATH = "/usr/lib/webcodex/webcodex-desktop"
STEP_IDS = (
    "artifact",
    "package_metadata",
    "preexisting_units",
    "preexisting_package",
    "install",
    "binaries",
    "runner_prereqs",
    "upgrade",
    "services_after_install",
    "systemd_start",
    "health",
    "restart",
    "uninstall",
    "residue",
    "reboot",
)
REFUSAL = "refusing install, purge, and service changes without a disposable environment label"
REBOOT_REASON = "reboot persistence is manual; the harness does not reboot"
REBOOT_PROCEDURE = (
    "Reboot the same disposable host, then confirm the package is installed, "
    "the runtime binaries are executable, and any units enabled outside this "
    "harness are active. This harness does not reboot and does not enable units."
)
MAX_UNITS = 8
OUTPUT_LIMIT = 8192
ENV_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS",
    "XDG_SESSION_TYPE",
)


class AcceptanceError(RuntimeError):
    pass


@dataclass
class CommandResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class AcceptanceConfig:
    deb: Path | None
    previous_deb: Path | None
    execute: bool
    disposable_environment: str | None
    exercise_existing_units: bool
    uninstall_only: bool
    package: str | None
    timeout_seconds: float


def clip(text: str, limit: int = 300) -> str:
    text = text.replace("\x00", "")
    return text if len(text) <= limit else text[:limit]


def bound(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return clip(value, 1024)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 4:
        return clip(str(value), 300)
    if isinstance(value, list):
        return [bound(item, depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        return {clip(str(key), 80): bound(item, depth + 1) for key, item in list(value.items())[:32]}
    return clip(str(value), 300)


def is_mutating(argv: list[str]) -> bool:
    if not argv:
        return False
    if argv[0] == "dpkg":
        return any(flag in argv[1:] for flag in DPKG_MUTATIONS)
    if argv[0] == "systemctl":
        return bool(SYSTEMCTL_MUTATIONS.intersection(argv[1:]))
    return False


def valid_label(label: str | None) -> bool:
    return bool(label) and LABEL_RE.fullmatch(label) is not None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_os_release(text: str | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip().lower()] = value.strip().strip('"').strip("'")
    return fields


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().lower()] = value.strip()
    return fields


def binary_paths(package: str) -> list[str]:
    paths = list(RUNTIME_PATHS)
    if package == "webcodex":
        paths.append(DESKTOP_PATH)
    return paths


def ubuntu_supported(version: str) -> bool:
    parts = version.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return False
    return (major, minor) >= (22, 4)


def matrix_target(host: dict[str, Any]) -> str | None:
    if host.get("containerized") is not False:
        return None
    distro = str(host.get("id") or "").casefold()
    version = str(host.get("version_id") or "")
    arch = {"amd64": "x64", "arm64": "arm64"}.get(str(host.get("dpkg_arch") or ""))
    if arch is None:
        return None
    if distro == "debian" and (version == "12" or version.startswith("12.")):
        return f"Debian 12 {arch}, .deb"
    if distro == "ubuntu" and ubuntu_supported(version):
        return f"Ubuntu 22.04+ {arch}, .deb"
    return None


_UPSTREAM_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.+-~")
_REVISION_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.+~")


def _version_order(char: str) -> int:
    """deb-version(7) order: tilde, then end-of-string, then letters, then other."""
    if not char or char.isdigit():
        return 0
    if char == "~":
        return -1
    if char.isalpha():
        return ord(char)
    return ord(char) + 256


def _verrevcmp(left: str, right: str) -> int:
    ia = ib = 0
    while ia < len(left) or ib < len(right):
        while (ia < len(left) and not left[ia].isdigit()) or (ib < len(right) and not right[ib].isdigit()):
            ac = _version_order(left[ia] if ia < len(left) else "")
            bc = _version_order(right[ib] if ib < len(right) else "")
            if ac != bc:
                return 1 if ac > bc else -1
            if ia < len(left):
                ia += 1
            if ib < len(right):
                ib += 1
        while ia < len(left) and left[ia] == "0":
            ia += 1
        while ib < len(right) and right[ib] == "0":
            ib += 1
        first = 0
        while ia < len(left) and left[ia].isdigit() and ib < len(right) and right[ib].isdigit():
            if first == 0:
                first = ord(left[ia]) - ord(right[ib])
            ia += 1
            ib += 1
        if ia < len(left) and left[ia].isdigit():
            return 1
        if ib < len(right) and right[ib].isdigit():
            return -1
        if first:
            return 1 if first > 0 else -1
    return 0


def split_debian_version(value: str) -> tuple[int, str, str] | None:
    """Split [epoch:]upstream[-revision] the way dpkg does, or reject an illegal version."""
    if not value or any(char.isspace() for char in value):
        return None
    epoch = 0
    rest = value
    if ":" in value:
        head, rest = value.split(":", 1)
        if not head.isdigit():
            return None
        epoch = int(head)
    if not rest or ":" in rest:
        return None
    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
    else:
        upstream, revision = rest, ""
    if not upstream or not upstream[0].isdigit():
        return None
    if any(char not in _UPSTREAM_CHARS for char in upstream):
        return None
    if any(char not in _REVISION_CHARS for char in revision):
        return None
    return epoch, upstream, revision


def dpkg_version_relation(left: str, right: str) -> int | None:
    """Compare versions with `dpkg --compare-versions` semantics.

    Returns -1, 0, or 1. Returns None when either version is illegal, so a
    caller cannot treat an uncomparable pair as an upgrade.
    """
    parsed_left = split_debian_version(left)
    parsed_right = split_debian_version(right)
    if parsed_left is None or parsed_right is None:
        return None
    if parsed_left[0] != parsed_right[0]:
        return 1 if parsed_left[0] > parsed_right[0] else -1
    relation = _verrevcmp(parsed_left[1], parsed_right[1])
    if relation == 0:
        relation = _verrevcmp(parsed_left[2], parsed_right[2])
    return relation


def is_newer(candidate: str, previous: str) -> bool:
    """True only when `dpkg --compare-versions candidate gt previous` is true."""
    return dpkg_version_relation(candidate, previous) == 1


def container_signals(read_text, environ: dict[str, str], exists) -> bool:
    if any(exists(path) for path in ("/.dockerenv", "/run/.containerenv")):
        return True
    if environ.get("container", "") in {"docker", "podman", "lxc", "systemd-nspawn"}:
        return True
    if (read_text("/run/systemd/container") or "").strip():
        return True
    cgroup = (read_text("/proc/1/cgroup") or "").lower()
    return any(marker in cgroup for marker in ("docker", "kubepods", "containerd", "libpod", "podman", "/lxc/"))


def candidate_flags(host: dict[str, Any], statuses: dict[str, str], host_execution: bool) -> dict[str, Any]:
    """Say whether this run is evidence a reviewer could attach to one matrix row.

    The checklist never writes the matrix. NOT_APPLICABLE and UNTESTED are not
    passes. Containers and fake runners never qualify. systemd_acceptance_candidate
    is start, health, and restart only; reboot stays UNTESTED.
    """

    target = matrix_target(host) if host_execution is True else None

    def passed(*step_ids: str) -> bool:
        return all(statuses.get(step_id) == "PASS" for step_id in step_ids)

    qualified = target is not None
    installer = qualified and passed("install", "binaries", "uninstall", "residue")
    upgrade = qualified and passed("upgrade")
    systemd = qualified and passed("systemd_start", "health", "restart")
    return {
        "installer_acceptance_candidate": installer,
        "upgrade_acceptance_candidate": upgrade,
        "systemd_acceptance_candidate": systemd,
        "candidate_target": target if installer or upgrade or systemd else None,
        "matrix_updated": False,
    }


def scrubbed_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if os.environ.get(key)}
    env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["DEBIAN_FRONTEND"] = "noninteractive"
    return env


def clip_output(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + "\n[truncated]"


def kill_pgid(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class _BoundedCapture:
    """Keep at most `limit` characters while a reader continues to drain."""

    def __init__(self, limit: int):
        self.limit = limit
        self._parts: list[str] = []
        self._kept = 0
        self._truncated = False

    def add(self, chunk: str) -> None:
        if not chunk or self._kept >= self.limit:
            if chunk:
                self._truncated = True
            return
        room = self.limit - self._kept
        self._parts.append(chunk[:room])
        self._kept += min(len(chunk), room)
        if len(chunk) > room:
            self._truncated = True

    def text(self) -> str:
        body = "".join(self._parts)
        if self._truncated:
            return body + "\n[truncated]"
        return body


def _drain(stream: Any, capture: _BoundedCapture) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            capture.add(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _readers_finished(readers: list[threading.Thread], budget: float) -> bool:
    if budget <= 0:
        return all(not reader.is_alive() for reader in readers)
    deadline = time.monotonic() + budget
    for reader in readers:
        reader.join(max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            return False
    return True


def _close_streams(proc: subprocess.Popen) -> None:
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _finish(proc: subprocess.Popen, readers: list[threading.Thread], pgid: int, *, kill: bool) -> None:
    """Bounded cleanup. A kill reaches the owned group even after its leader has exited."""
    if kill:
        kill_pgid(pgid)
    if proc.poll() is None:
        try:
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            kill_pgid(pgid)
    deadline = time.monotonic() + 1
    for reader in readers:
        try:
            reader.join(max(0.0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            kill_pgid(pgid)
    if any(reader.is_alive() for reader in readers):
        _close_streams(proc)
        deadline = time.monotonic() + 1
        for reader in readers:
            reader.join(max(0.0, deadline - time.monotonic()))
    else:
        _close_streams(proc)
    if proc.poll() is None:
        kill_pgid(pgid)
        try:
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            pass


class SubprocessRunner:
    def __init__(self, allow_mutation: bool = False):
        self.allow_mutation = allow_mutation

    def run(self, argv: list[str], timeout: float) -> CommandResult:
        if is_mutating(argv) and not self.allow_mutation:
            raise AcceptanceError("refusing mutating command without disposable authorization")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
                env=scrubbed_env(),
            )
        except OSError as exc:
            return CommandResult(127, "", str(exc))
        stdout_capture = _BoundedCapture(OUTPUT_LIMIT)
        stderr_capture = _BoundedCapture(OUTPUT_LIMIT)
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout_capture), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr_capture), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        deadline = time.monotonic() + float(timeout)
        timed_out = False
        try:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
            else:
                if not _readers_finished(readers, deadline - time.monotonic()):
                    timed_out = True
            _finish(proc, readers, pgid, kill=timed_out)
        except BaseException:
            _finish(proc, readers, pgid, kill=True)
            raise
        stdout = stdout_capture.text()
        stderr = stderr_capture.text()
        if timed_out:
            return CommandResult(None, stdout, stderr or "still running", timed_out=True)
        return CommandResult(proc.returncode, stdout, stderr)


class LocalHostView:
    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return None

    def executable(self, path: str) -> bool:
        return os.access(path, os.X_OK)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def containerized(self) -> bool:
        try:
            return container_signals(self.read_text, os.environ, os.path.exists)
        except OSError:
            return True


class ReportBuilder:
    def __init__(self, previous_supplied: bool):
        self.previous_supplied = previous_supplied
        self.steps: dict[str, dict[str, Any]] = {}
        self.code = 0
        self.stopped = False
        self.mutated = False

    def put(self, step_id: str, status: str, exit_code: int | None = None, reason: str = "", evidence: dict | None = None) -> None:
        self.steps[step_id] = {
            "id": step_id,
            "status": status,
            "exit_code": exit_code,
            "reason": clip(reason),
            "evidence": bound(evidence or {}),
        }

    def fail(self, step_id: str, reason: str, exit_code: int | None = None, evidence: dict | None = None) -> None:
        self.put(step_id, "FAIL", exit_code=exit_code, reason=reason, evidence=evidence)
        if self.code == 0:
            self.code = 2
        self.stopped = True

    def interrupt(self, step_id: str) -> None:
        self.put(step_id, "INTERRUPTED", reason="interrupted")
        self.code = 130
        self.stopped = True

    def ensure_defaults(self) -> None:
        for step_id in STEP_IDS:
            if step_id in self.steps:
                continue
            if step_id == "reboot":
                self.put(step_id, "UNTESTED", reason=REBOOT_REASON, evidence={"procedure": REBOOT_PROCEDURE})
            elif step_id == "upgrade" and not self.previous_supplied:
                self.put(step_id, "UNTESTED", reason="no previous package supplied")
            else:
                self.put(step_id, "NOT_RUN")

    def ordered(self) -> list[dict[str, Any]]:
        return [self.steps[step_id] for step_id in STEP_IDS]


def host_from_view(view: Any) -> dict[str, Any]:
    fields = parse_os_release(view.read_text("/etc/os-release"))
    return {
        "id": fields.get("id"),
        "version_id": fields.get("version_id"),
        "dpkg_arch": None,
        "containerized": bool(view.containerized()),
    }


def command_evidence(argv: list[str], result: CommandResult) -> dict[str, Any]:
    return {"argv0": argv[0] if argv else "", "stdout": result.stdout, "stderr": result.stderr}


def run_command(builder: ReportBuilder, runner: Any, timeout: float, step_id: str, argv: list[str]) -> CommandResult | None:
    if is_mutating(argv):
        builder.mutated = True
    try:
        result = runner.run(list(argv), float(timeout))
    except KeyboardInterrupt:
        builder.interrupt(step_id)
        return None
    if not isinstance(result, CommandResult):
        builder.fail(step_id, "command runner returned no result")
        return None
    if result.timed_out or result.exit_code is None:
        detail = clip(result.stderr or result.stdout or "still running", 200)
        builder.fail(step_id, f"timed out: {detail}", exit_code=None, evidence=command_evidence(argv, result))
        return None
    return result


def require_ok(builder: ReportBuilder, result: CommandResult, step_id: str, empty_reason: str, hint: str = "") -> bool:
    if result.exit_code == 0:
        return True
    builder.fail(step_id, clip(f"{result.stderr or result.stdout or empty_reason}{hint}"), exit_code=result.exit_code, evidence={"stdout": result.stdout, "stderr": result.stderr})
    return False


def path_exists(view: Any, path: str) -> bool:
    exists = getattr(view, "exists", None)
    if callable(exists):
        return bool(exists(path))
    return bool(view.executable(path))


def list_argv(scope: str) -> list[str]:
    if scope == "user":
        return ["systemctl", "--user", "list-unit-files", "webcodex*.service", "--no-ask-password"]
    return ["systemctl", "list-unit-files", "webcodex*.service", "--no-ask-password"]


def systemctl_argv(scope: str, verb: str, unit: str) -> list[str]:
    if scope == "user":
        return ["systemctl", "--user", verb, unit, "--no-ask-password"]
    return ["systemctl", verb, unit, "--no-ask-password"]


def bus_unavailable(result: CommandResult) -> bool:
    text = f"{result.stderr}\n{result.stdout}"
    return result.exit_code not in (0, None) and (
        "Failed to connect to bus" in text or "not been booted" in text or "Failed to connect to user scope" in text
    )


def parse_units(stdout: str, scope: str) -> list[dict[str, str]]:
    units = []
    for line in stdout.splitlines():
        match = UNIT_RE.match(line.strip())
        if match:
            units.append({"name": match.group(1), "state": match.group(2), "scope": scope})
    return units


def probe_units(builder: ReportBuilder, runner: Any, timeout: float, step_id: str) -> list[dict[str, str]] | None:
    found: list[dict[str, str]] = []
    for scope in ("system", "user"):
        result = run_command(builder, runner, timeout, step_id, list_argv(scope))
        if builder.stopped or result is None:
            return None
        if scope == "user" and bus_unavailable(result):
            continue
        if result.exit_code != 0:
            builder.fail(step_id, clip(result.stderr or "systemctl list-unit-files failed"), exit_code=result.exit_code)
            return None
        found.extend(parse_units(result.stdout, scope))
    if len(found) > MAX_UNITS:
        builder.fail(step_id, f"refusing more than {MAX_UNITS} webcodex units")
        return None
    return found


def dpkg_deb_missing(result: CommandResult) -> bool:
    if result.exit_code in (0, None):
        return False
    if result.exit_code == 127:
        return True
    text = f"{result.stderr}\n{result.stdout}".casefold()
    return "command not found" in text


def load_metadata(builder: ReportBuilder, runner: Any, timeout: float, path: Path, *, missing_is_untested: bool) -> dict[str, str] | None:
    """Record package validity separately from the artifact hash."""
    argv = ["dpkg-deb", "-f", str(path)]
    try:
        result = runner.run(argv, float(timeout))
    except KeyboardInterrupt:
        builder.interrupt("package_metadata")
        return None
    except AcceptanceError as exc:
        builder.fail("package_metadata", str(exc), evidence={"package_validated": False})
        return None
    if not isinstance(result, CommandResult):
        builder.fail("package_metadata", "command runner returned no result", evidence={"package_validated": False})
        return None
    if result.timed_out or result.exit_code is None:
        detail = clip(result.stderr or result.stdout or "still running", 200)
        builder.fail("package_metadata", f"timed out: {detail}", evidence={"package_validated": False, "stdout": result.stdout, "stderr": result.stderr})
        return None
    if dpkg_deb_missing(result):
        evidence = {"package_validated": False, "stderr": result.stderr}
        reason = "dpkg-deb is unavailable; package metadata was not verified"
        if missing_is_untested:
            builder.put("package_metadata", "UNTESTED", exit_code=result.exit_code, reason=reason, evidence=evidence)
        else:
            builder.fail("package_metadata", reason, exit_code=result.exit_code, evidence=evidence)
        return None
    if result.exit_code != 0:
        builder.fail(
            "package_metadata",
            clip(result.stderr or result.stdout or "dpkg-deb failed"),
            exit_code=result.exit_code,
            evidence={"package_validated": False, "stdout": result.stdout, "stderr": result.stderr},
        )
        return None
    fields = parse_control(result.stdout)
    package = fields.get("package", "")
    if package not in ALLOWED_PACKAGES or not fields.get("version") or not fields.get("architecture"):
        builder.fail(
            "package_metadata",
            f"unsupported or incomplete package control for {package or 'unknown'}",
            exit_code=result.exit_code,
            evidence={"package_validated": False, "stdout": result.stdout},
        )
        return None
    builder.put(
        "package_metadata",
        "PASS",
        exit_code=0,
        evidence={
            "package": package,
            "version": fields["version"],
            "architecture": fields["architecture"],
            "package_validated": True,
        },
    )
    return fields


def guard_existing_install(builder: ReportBuilder, runner: Any, timeout: float, package: str) -> bool:
    """Refuse a normal lifecycle that would overwrite and then purge an existing install."""
    names = (package, CONFLICT_PACKAGES[package])
    for name in names:
        result = run_command(builder, runner, timeout, "preexisting_package", ["dpkg-query", "-W", name])
        if builder.stopped or result is None:
            return False
        if result.exit_code == 0:
            builder.fail(
                "preexisting_package",
                f"{name} is already installed; refusing to overwrite and purge an existing installation",
                exit_code=result.exit_code,
                evidence={"package": name, "stdout": result.stdout},
            )
            return False
        if result.exit_code != 1:
            builder.fail(
                "preexisting_package",
                clip(result.stderr or result.stdout or f"dpkg-query failed with exit {result.exit_code}"),
                exit_code=result.exit_code,
                evidence={"package": name, "stdout": result.stdout, "stderr": result.stderr},
            )
            return False
    builder.put("preexisting_package", "PASS", evidence={"absent": list(names)})
    return True


def read_deb(builder: ReportBuilder, runner: Any, timeout: float, path: Path) -> dict[str, str] | None:
    result = run_command(builder, runner, timeout, "install", ["dpkg-deb", "-f", str(path)])
    if builder.stopped or result is None:
        return None
    if not require_ok(builder, result, "install", "dpkg-deb failed"):
        return None
    fields = parse_control(result.stdout)
    package = fields.get("package", "")
    if package not in ALLOWED_PACKAGES or not fields.get("version") or not fields.get("architecture"):
        builder.fail("install", f"unsupported or incomplete package control for {package or 'unknown'}")
        return None
    return fields


def read_arch(builder: ReportBuilder, runner: Any, timeout: float, host: dict[str, Any], step_id: str) -> str | None:
    result = run_command(builder, runner, timeout, step_id, ["dpkg", "--print-architecture"])
    if builder.stopped or result is None:
        return None
    if not require_ok(builder, result, step_id, "dpkg --print-architecture failed"):
        return None
    host["dpkg_arch"] = clip(result.stdout.strip(), 64)
    return host["dpkg_arch"]


def architectures_match(fields: dict[str, str], host_arch: str) -> bool:
    return fields.get("architecture", "") == host_arch


def install_deb(builder: ReportBuilder, runner: Any, timeout: float, path: Path, step_id: str) -> CommandResult | None:
    result = run_command(builder, runner, timeout, step_id, ["dpkg", "-i", str(path)])
    if builder.stopped or result is None:
        return None
    if not require_ok(builder, result, step_id, "dpkg install failed", "; recover with --uninstall-only if a partial install remains"):
        return None
    return result


def record_binaries(builder: ReportBuilder, view: Any, package: str) -> bool:
    paths = binary_paths(package)
    missing = [path for path in paths if not view.executable(path)]
    if missing:
        builder.fail("binaries", "installed binaries missing: " + ", ".join(missing), evidence={"missing": missing})
        return False
    builder.put("binaries", "PASS", evidence={"paths": paths})
    runtime_missing = [path for path in RUNTIME_PATHS if not view.executable(path)]
    if runtime_missing:
        builder.fail("runner_prereqs", "runtime binaries missing", evidence={"missing": runtime_missing})
        return False
    builder.put("runner_prereqs", "PASS", evidence={
        "executed": False,
        "credentials_used": False,
        "paths": list(RUNTIME_PATHS),
        "note": "executable presence only; this step does not run the binaries",
    })
    return True


def record_service_plan(builder: ReportBuilder, before: list[dict[str, str]], after: list[dict[str, str]]) -> list[dict[str, str]] | None:
    before_names = {unit["name"] for unit in before}
    introduced = [unit["name"] for unit in after if unit["name"] not in before_names]
    if introduced:
        builder.fail("services_after_install", "package install introduced webcodex units", evidence={"units": introduced})
        return None
    builder.put("services_after_install", "PASS", evidence={
        "units": [unit["name"] for unit in after],
        "note": "no new webcodex units after install; fresh packages do not ship units",
    })
    if after:
        return after
    reason = "fresh package does not ship systemd units"
    for step_id in ("systemd_start", "health", "restart"):
        builder.put(step_id, "NOT_APPLICABLE", reason=reason)
    return []


def stop_started(builder: ReportBuilder, runner: Any, timeout: float, units: list[dict[str, str]]) -> None:
    for unit in units:
        result = run_command(builder, runner, timeout, "uninstall", systemctl_argv(unit["scope"], "stop", unit["name"]))
        if result is None or builder.steps.get("uninstall", {}).get("status") in {"FAIL", "INTERRUPTED"}:
            return
        if result.exit_code != 0:
            builder.fail("uninstall", clip(result.stderr or f"stop failed for {unit['name']}"), exit_code=result.exit_code)
            return


def exercise_units(builder: ReportBuilder, runner: Any, timeout: float, units: list[dict[str, str]]) -> None:
    started: list[dict[str, str]] = []
    names: list[str] = []
    for unit in units:
        start = run_command(builder, runner, timeout, "systemd_start", systemctl_argv(unit["scope"], "start", unit["name"]))
        if builder.stopped or start is None:
            stop_started(builder, runner, timeout, started)
            return
        if not require_ok(builder, start, "systemd_start", f"start failed for {unit['name']}"):
            stop_started(builder, runner, timeout, started)
            return
        started.append(unit)
        names.append(unit["name"])
        active = run_command(builder, runner, timeout, "health", systemctl_argv(unit["scope"], "is-active", unit["name"]))
        if builder.stopped or active is None:
            if builder.steps.get("systemd_start", {}).get("status") != "FAIL":
                builder.put("systemd_start", "PASS", exit_code=0, evidence={"units": names, "note": "start returned success; enable was not called"})
            stop_started(builder, runner, timeout, started)
            return
        if active.exit_code != 0 or active.stdout.strip() != "active":
            builder.put("systemd_start", "PASS", exit_code=0, evidence={"units": names, "note": "start returned success; enable was not called"})
            builder.fail("health", f"{unit['name']} is {active.stdout.strip() or 'inactive'}", exit_code=active.exit_code)
            stop_started(builder, runner, timeout, started)
            return
    builder.put("systemd_start", "PASS", exit_code=0, evidence={"units": names, "note": "start returned success; enable was not called"})
    builder.put("health", "PASS", exit_code=0, evidence={"check": "is-active", "units": names, "credentials_used": False})
    for unit in units:
        restart = run_command(builder, runner, timeout, "restart", systemctl_argv(unit["scope"], "restart", unit["name"]))
        if builder.stopped or restart is None:
            stop_started(builder, runner, timeout, started)
            return
        if not require_ok(builder, restart, "restart", f"restart failed for {unit['name']}"):
            stop_started(builder, runner, timeout, started)
            return
        active = run_command(builder, runner, timeout, "restart", systemctl_argv(unit["scope"], "is-active", unit["name"]))
        if builder.stopped or active is None:
            stop_started(builder, runner, timeout, started)
            return
        if active.exit_code != 0 or active.stdout.strip() != "active":
            builder.fail("restart", f"{unit['name']} is {active.stdout.strip() or 'inactive'} after restart", exit_code=active.exit_code)
            stop_started(builder, runner, timeout, started)
            return
    builder.put("restart", "PASS", exit_code=0, evidence={"units": names, "note": "enable was not called"})
    stop_started(builder, runner, timeout, started)


def purge_and_residue(builder: ReportBuilder, runner: Any, view: Any, timeout: float, package: str, before: list[dict[str, str]]) -> None:
    result = run_command(builder, runner, timeout, "uninstall", ["dpkg", "--purge", package])
    if builder.stopped or result is None:
        return
    if not require_ok(builder, result, "uninstall", "dpkg purge failed"):
        return
    builder.put("uninstall", "PASS", exit_code=result.exit_code, evidence={"package": package})
    query = run_command(builder, runner, timeout, "residue", ["dpkg-query", "-W", package])
    if builder.stopped or query is None:
        return
    if query.exit_code not in (0, 1):
        builder.fail("residue", clip(query.stderr or f"dpkg-query failed with exit {query.exit_code}"), exit_code=query.exit_code, evidence={"stdout": query.stdout, "stderr": query.stderr})
        return
    remaining = [path for path in binary_paths(package) if path_exists(view, path)]
    installed = query.exit_code == 0
    after = probe_units(builder, runner, timeout, "residue")
    if builder.stopped or after is None:
        return
    before_names = {unit["name"] for unit in before}
    unexpected = [unit["name"] for unit in after if unit["name"] not in before_names]
    evidence = {
        "dpkg_query_exit_code": query.exit_code,
        "binaries": remaining,
        "unexpected_units": unexpected,
        "retained_preexisting_units": [unit["name"] for unit in after if unit["name"] in before_names],
        "note": "/var/lib/webcodex-installer may remain and is not treated as package residue",
    }
    if installed or remaining or unexpected:
        reason = "package still installed" if installed else "unexpected webcodex unit remains" if unexpected else "package-owned path remains"
        builder.fail("residue", reason, exit_code=query.exit_code, evidence=evidence)
        return
    builder.put("residue", "PASS", exit_code=query.exit_code, evidence=evidence)


def guard_units(builder: ReportBuilder, units: list[dict[str, str]], exercise: bool) -> bool:
    names = [unit["name"] for unit in units]
    if names and not exercise:
        builder.fail("preexisting_units", "preexisting webcodex units require --exercise-existing-units", evidence={"units": names})
        return False
    builder.put("preexisting_units", "PASS", evidence={"units": names})
    return True


def run_plan(config: AcceptanceConfig, runner: Any, builder: ReportBuilder) -> None:
    if config.uninstall_only:
        builder.put("artifact", "NOT_RUN", reason="uninstall-only recovery does not require a package")
        return
    deb = config.deb
    if deb is None or not deb.is_file():
        builder.fail("artifact", "deb not found")
        return
    builder.put("artifact", "PASS", evidence={"sha256": sha256_file(deb), "bytes": deb.stat().st_size})
    load_metadata(builder, runner, config.timeout_seconds, deb, missing_is_untested=True)
    if builder.stopped:
        return
    if config.previous_deb is not None:
        builder.put("upgrade", "NOT_RUN", reason="previous package supplied but execute was not requested")


def run_uninstall_only(config: AcceptanceConfig, runner: Any, view: Any, builder: ReportBuilder, host: dict[str, Any]) -> None:
    package = config.package or ""
    if package not in ALLOWED_PACKAGES:
        builder.fail("uninstall", f"unsupported package {package or 'unknown'}")
        return
    if read_arch(builder, runner, config.timeout_seconds, host, "uninstall") is None:
        return
    before = probe_units(builder, runner, config.timeout_seconds, "preexisting_units")
    if before is None or not guard_units(builder, before, config.exercise_existing_units):
        return
    purge_and_residue(builder, runner, view, config.timeout_seconds, package, before)


def run_execute(config: AcceptanceConfig, runner: Any, view: Any, builder: ReportBuilder, host: dict[str, Any]) -> None:
    deb = config.deb
    if deb is None or not deb.is_file():
        builder.fail("artifact", "deb not found")
        return
    builder.put("artifact", "PASS", evidence={"sha256": sha256_file(deb), "bytes": deb.stat().st_size})
    current = load_metadata(builder, runner, config.timeout_seconds, deb, missing_is_untested=False)
    if current is None:
        return
    evidence = dict(builder.steps["artifact"]["evidence"])
    evidence.update(package=current.get("package"), version=current.get("version"), architecture=current.get("architecture"))
    builder.steps["artifact"]["evidence"] = bound(evidence)
    previous = None
    if config.previous_deb is not None:
        if not config.previous_deb.is_file():
            builder.fail("upgrade", "previous deb not found")
            return
        previous = read_deb(builder, runner, config.timeout_seconds, config.previous_deb)
        if previous is None:
            return
        if previous.get("package") != current.get("package"):
            builder.fail("upgrade", "previous package does not match the candidate package")
            return
        if not is_newer(str(current.get("version") or ""), str(previous.get("version") or "")):
            builder.fail("upgrade", f"candidate version {current.get('version')} is not newer than previous version {previous.get('version')}")
            return
    host_arch = read_arch(builder, runner, config.timeout_seconds, host, "install")
    if host_arch is None:
        return
    before = probe_units(builder, runner, config.timeout_seconds, "preexisting_units")
    if before is None or not guard_units(builder, before, config.exercise_existing_units):
        return
    for fields, label in ((current, "candidate"), (previous, "previous")):
        if fields is not None and not architectures_match(fields, host_arch):
            builder.fail("install", f"package architecture {fields.get('architecture')} does not match host architecture {host_arch}", evidence={"artifact": label})
            return
    package = current["package"]
    if not guard_existing_install(builder, runner, config.timeout_seconds, package):
        return
    if previous is not None and config.previous_deb is not None:
        if install_deb(builder, runner, config.timeout_seconds, config.previous_deb, "install") is None:
            return
        builder.put("install", "PASS", exit_code=0, evidence={"deb": str(config.previous_deb), "version": previous.get("version")})
        upgraded = install_deb(builder, runner, config.timeout_seconds, deb, "upgrade")
        if upgraded is None:
            return
        builder.put("upgrade", "PASS", exit_code=upgraded.exit_code, evidence={"from_version": previous.get("version"), "to_version": current.get("version"), "deb": str(deb)})
    else:
        installed = install_deb(builder, runner, config.timeout_seconds, deb, "install")
        if installed is None:
            return
        builder.put("install", "PASS", exit_code=installed.exit_code, evidence={"deb": str(deb), "version": current.get("version"), "package": package})
    if not record_binaries(builder, view, package):
        return
    after = probe_units(builder, runner, config.timeout_seconds, "services_after_install")
    if after is None:
        return
    exercisable = record_service_plan(builder, before, after)
    if exercisable is None:
        return
    if exercisable:
        exercise_units(builder, runner, config.timeout_seconds, exercisable)
        if builder.stopped:
            return
    purge_and_residue(builder, runner, view, config.timeout_seconds, package, before)


def assemble(builder: ReportBuilder, host: dict[str, Any], runner: Any, view: Any, refused: str) -> dict[str, Any]:
    statuses = {step["id"]: step["status"] for step in builder.ordered()}
    host_execution = isinstance(runner, SubprocessRunner) and isinstance(view, LocalHostView)
    flags = candidate_flags(host, statuses, host_execution)
    return {
        "executed": builder.mutated,
        "matrix_updated": False,
        "candidate_target": flags["candidate_target"],
        "refused": refused,
        "host": host,
        "steps": builder.ordered(),
        "installer_acceptance_candidate": flags["installer_acceptance_candidate"],
        "systemd_acceptance_candidate": flags["systemd_acceptance_candidate"],
        "upgrade_acceptance_candidate": flags["upgrade_acceptance_candidate"],
    }


def acceptance(config: AcceptanceConfig, runner: Any, view: Any) -> tuple[dict[str, Any], int]:
    builder = ReportBuilder(previous_supplied=config.previous_deb is not None)
    host = host_from_view(view)
    if config.execute and not valid_label(config.disposable_environment):
        builder.ensure_defaults()
        return assemble(builder, host, runner, view, REFUSAL), 3
    if not config.execute:
        run_plan(config, runner, builder)
    elif config.uninstall_only:
        run_uninstall_only(config, runner, view, builder, host)
    else:
        run_execute(config, runner, view, builder, host)
    builder.ensure_defaults()
    return assemble(builder, host, runner, view, ""), builder.code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deb", nargs="?", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--disposable-environment", default=None)
    parser.add_argument("--previous-deb", type=Path)
    parser.add_argument("--exercise-existing-units", action="store_true")
    parser.add_argument("--uninstall-only", action="store_true")
    parser.add_argument("--package", choices=ALLOWED_PACKAGES)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 180:
        print("timeout must be greater than 0 and at most 180 seconds", file=sys.stderr)
        return 1
    if args.uninstall_only and not args.package:
        print("--package is required with --uninstall-only", file=sys.stderr)
        return 1
    if not args.uninstall_only and (args.deb is None or not args.deb.is_file()):
        print("a .deb path is required", file=sys.stderr)
        return 1
    authorized = bool(args.execute and valid_label(args.disposable_environment))
    config = AcceptanceConfig(
        deb=args.deb,
        previous_deb=args.previous_deb,
        execute=args.execute,
        disposable_environment=args.disposable_environment,
        exercise_existing_units=args.exercise_existing_units,
        uninstall_only=args.uninstall_only,
        package=args.package,
        timeout_seconds=args.timeout,
    )
    try:
        report, code = acceptance(config, SubprocessRunner(allow_mutation=authorized), LocalHostView())
    except AcceptanceError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    text = json.dumps(report, indent=2) + "\n"
    if args.report is not None:
        args.report.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    if report.get("refused"):
        print(report["refused"], file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
