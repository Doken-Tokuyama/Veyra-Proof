import json
from pathlib import Path

from veyra_proof.command_runner import CommandRunner
from veyra_proof.models import CommandResult
from veyra_proof.workspace import WorkspaceError, resolve_workspace


def test_resolve_workspace_from_metadata(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\nedition="2021"\n',
        encoding="utf-8",
    )
    fixture = Path(__file__).parent / "fixtures" / "cargo_metadata_sample.json"
    sample = json.loads(fixture.read_text(encoding="utf-8"))
    sample["workspace_root"] = str(root)
    sample["target_directory"] = str(root / "target")

    def fake_run(self, command, **kwargs):
        assert command[:2] == ["cargo", "metadata"]
        return CommandResult(
            command=command,
            cwd=str(root),
            exit_code=0,
            stdout=json.dumps(sample),
            stderr="",
            duration_seconds=0.01,
        )

    monkeypatch.setattr(CommandRunner, "run", fake_run)
    info = resolve_workspace(root, CommandRunner())
    assert info.workspace_root == str(root.resolve())
    assert info.packages[0]["name"] == "demo"


def test_missing_cargo_toml(tmp_path):
    try:
        resolve_workspace(tmp_path, CommandRunner(dry_run=True))
        raise AssertionError("expected WorkspaceError")
    except WorkspaceError as exc:
        assert exc.exit_code == 2
