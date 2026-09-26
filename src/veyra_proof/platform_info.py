"""Platform and toolchain detection."""

from __future__ import annotations

import platform
import shutil
from dataclasses import asdict, dataclass, field
from typing import Any

from veyra_proof.command_runner import CommandRunner
from veyra_proof.models import PlatformOS


def normalize_os(system: str | None = None) -> PlatformOS:
    name = (system or platform.system()).lower()
    if name == "windows":
        return PlatformOS.WINDOWS
    if name == "darwin":
        return PlatformOS.MACOS
    if name == "linux":
        return PlatformOS.LINUX
    return PlatformOS.OTHER


def normalize_arch(machine: str | None = None) -> str:
    raw = (machine or platform.machine() or "").lower()
    mapping = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "x64": "x86_64",
        "i386": "x86",
        "i686": "x86",
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "armv7l": "armv7",
        "armv8l": "aarch64",
    }
    return mapping.get(raw, raw or "unknown")


@dataclass
class PlatformInfo:
    os: PlatformOS
    os_raw: str
    architecture: str
    architecture_raw: str
    python_version: str
    python_executable: str
    cargo_path: str | None = None
    cargo_version: str | None = None
    rustc_path: str | None = None
    rustc_version: str | None = None
    rustup_path: str | None = None
    rustup_version: str | None = None
    active_toolchain: str | None = None
    nightly_available: bool = False
    rustup_components: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["os"] = self.os.value
        return data

    def is_supported(self) -> bool:
        return self.os in {PlatformOS.WINDOWS, PlatformOS.MACOS, PlatformOS.LINUX}


def detect_platform(runner: CommandRunner | None = None) -> PlatformInfo:
    import sys

    runner = runner or CommandRunner()
    info = PlatformInfo(
        os=normalize_os(),
        os_raw=platform.system(),
        architecture=normalize_arch(),
        architecture_raw=platform.machine(),
        python_version=platform.python_version(),
        python_executable=sys.executable,
        cargo_path=shutil.which("cargo"),
        rustc_path=shutil.which("rustc"),
        rustup_path=shutil.which("rustup"),
    )

    if info.cargo_path:
        r = runner.run([info.cargo_path, "--version"], timeout=15)
        if r.exit_code == 0 and not r.timed_out:
            info.cargo_version = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None

    if info.rustc_path:
        r = runner.run([info.rustc_path, "--version"], timeout=15)
        if r.exit_code == 0 and not r.timed_out:
            info.rustc_version = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None

    if info.rustup_path:
        r = runner.run([info.rustup_path, "--version"], timeout=15)
        if r.exit_code == 0 and not r.timed_out:
            info.rustup_version = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None

        show = runner.run([info.rustup_path, "show", "active-toolchain"], timeout=20)
        if show.exit_code == 0 and not show.timed_out and show.stdout.strip():
            info.active_toolchain = show.stdout.strip().splitlines()[0]

        night = runner.run([info.rustup_path, "toolchain", "list"], timeout=20)
        if night.exit_code == 0 and not night.timed_out:
            info.nightly_available = any("nightly" in line for line in night.stdout.splitlines())

        comps = runner.run([info.rustup_path, "component", "list", "--installed"], timeout=30)
        if comps.exit_code == 0 and not comps.timed_out:
            info.rustup_components = [ln.strip() for ln in comps.stdout.splitlines() if ln.strip()]

    return info
