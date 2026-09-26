from veyra_proof.hashing import compare_snapshots, snapshot_project_files
from veyra_proof.installer import confirm_installs
from veyra_proof.models import InstallPlan, InstallStep
from veyra_proof.reporting import classify_project_changes


def test_hash_and_change_detection(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    before = snapshot_project_files(tmp_path)
    (tmp_path / "Cargo.toml").write_text("[package]\nname='y'\n", encoding="utf-8")
    after = snapshot_project_files(tmp_path)
    changes = compare_snapshots(before, after)
    assert any(c["path"] == "Cargo.toml" and c["change"] == "modified" for c in changes)


def test_lockfile_creation_not_unauthorized():
    changes = [{"path": "Cargo.lock", "change": "created", "before_sha256": None, "after_sha256": "abc"}]
    annotated, unauthorized = classify_project_changes(
        changes,
        update_lockfile=False,
        allow_manifest_changes=False,
        lockfile_existed_before=False,
    )
    assert unauthorized == []
    assert annotated[0]["authorization"] == "informational_cargo_side_effect"


def test_lockfile_modification_unauthorized_without_flag():
    changes = [{"path": "Cargo.lock", "change": "modified", "before_sha256": "a", "after_sha256": "b"}]
    _, unauthorized = classify_project_changes(
        changes,
        update_lockfile=False,
        allow_manifest_changes=False,
        lockfile_existed_before=True,
    )
    assert "Cargo.lock" in unauthorized


def test_confirm_installs_yes():
    plan = InstallPlan(
        "clippy",
        "clippy",
        "rustup_component",
        None,
        [InstallStep("x", ["rustup", "component", "add", "clippy"])],
    )
    assert confirm_installs([plan], yes=True) is True


def test_confirm_installs_no():
    plan = InstallPlan(
        "clippy",
        "clippy",
        "rustup_component",
        None,
        [InstallStep("x", ["rustup", "component", "add", "clippy"])],
    )
    assert confirm_installs([plan], yes=False, prompt_fn=lambda m: "n") is False
