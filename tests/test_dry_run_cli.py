from veyra_proof.cli import main
from veyra_proof.command_runner import CommandRunner
from veyra_proof.models import CommandResult, PlatformOS, WorkspaceInfo
from veyra_proof.platform_info import PlatformInfo


def test_dry_run_audit(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\nedition="2021"\n',
        encoding="utf-8",
    )

    platform = PlatformInfo(
        os=PlatformOS.MACOS,
        os_raw="Darwin",
        architecture="aarch64",
        architecture_raw="arm64",
        python_version="3.12",
        python_executable="python",
        cargo_path="/fake/cargo",
        cargo_version="cargo 1.0",
        rustc_path="/fake/rustc",
        rustc_version="rustc 1.0",
        rustup_path="/fake/rustup",
        rustup_version="rustup 1.0",
        active_toolchain="stable",
        nightly_available=False,
    )

    ws = WorkspaceInfo(
        path=str(root),
        workspace_root=str(root),
        packages=[{"name": "demo", "id": "demo", "targets": [{"kind": ["lib"]}]}],
        workspace_members=["demo"],
        target_directory=str(root / "target"),
        default_members=["demo"],
        root_package={"name": "demo"},
        has_lockfile=False,
        lockfile_path=None,
        metadata_raw={},
    )

    monkeypatch.setattr("veyra_proof.cli.detect_platform", lambda runner=None: platform)
    monkeypatch.setattr("veyra_proof.cli.resolve_workspace", lambda path, runner=None: ws)

    executed = []

    def fake_run(self, command, **kwargs):
        executed.append(list(command))
        return CommandResult(command, str(root), 0, "", "", 0.0)

    monkeypatch.setattr(CommandRunner, "run", fake_run)
    code = main(["audit", str(root), "--dry-run", "--profile", "quick"])
    assert code == 0
    # Detection may probe --version; actual audit invocations must not run.
    banned = {
        ("cargo", "check", "--workspace", "--all-targets"),
        ("cargo", "test", "--workspace"),
        ("cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"),
        ("cargo", "fmt", "--all", "--", "--check"),
    }
    for cmd in executed:
        assert tuple(cmd) not in banned
