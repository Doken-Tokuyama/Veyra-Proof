"""A broken configuration is a usage error, and a typo is not a warning.

Measured before this file existed: a `veyra.toml` with a missing `=` exited 3 with
"INTERNAL ERROR" — the code that means *this tool has a bug* — and named no file, so
the one thing a reader needed (which line of which file) was absent. Separately,
`--include nope` printed a warning and then produced an INCONCLUSIVE audit whose
reason said the tools were "missing or not executable", which invites installing
binaries that are already installed.

Both are the same defect class this project exists to refuse: a message that is
confident and wrong. Fixing them is not cosmetic — exit 3 in CI means "the auditor
crashed", and somebody would go looking for the bug in the wrong program.
"""

from __future__ import annotations

import json

import pytest

from veyra_proof.cli import main
from veyra_proof.config import ConfigError, load_veyra_config
from veyra_proof.models import PlatformOS, WorkspaceInfo
from veyra_proof.platform_info import PlatformInfo
from veyra_proof.reporting import decide, unique_evidence_dir


def _platform() -> PlatformInfo:
    return PlatformInfo(
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


def _workspace(root) -> WorkspaceInfo:
    return WorkspaceInfo(
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


def _stub_cli(monkeypatch, root) -> None:
    monkeypatch.setattr("veyra_proof.cli.detect_platform", lambda runner=None: _platform())
    monkeypatch.setattr(
        "veyra_proof.cli.resolve_workspace", lambda path, runner=None: _workspace(root)
    )


@pytest.fixture()
def crate(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8"
    )
    return root


class TestBrokenConfigIsAUsageError:
    def test_malformed_toml_names_the_file_and_the_line(self, tmp_path):
        (tmp_path / "veyra.toml").write_text("this is not toml [\n", encoding="utf-8")

        with pytest.raises(ConfigError) as excinfo:
            load_veyra_config(tmp_path)

        message = str(excinfo.value)
        assert "veyra.toml" in message
        assert "line 1" in message

    def test_an_unusable_value_names_the_file(self, tmp_path):
        (tmp_path / "veyra.toml").write_text(
            '[mutants]\ntimeout_seconds = "soon"\n', encoding="utf-8"
        )

        with pytest.raises(ConfigError) as excinfo:
            load_veyra_config(tmp_path)

        assert "veyra.toml" in str(excinfo.value)

    def test_a_valid_file_still_loads(self, tmp_path):
        (tmp_path / "veyra.toml").write_text('[semver]\nbaseline = "v0.9.1"\n', encoding="utf-8")

        config = load_veyra_config(tmp_path)

        assert config.semver.baseline == "v0.9.1"

    def test_the_cli_exits_2_instead_of_calling_itself_broken(self, crate, monkeypatch, capsys):
        (crate / "veyra.toml").write_text("this is not toml [\n", encoding="utf-8")
        _stub_cli(monkeypatch, crate)

        code = main(["audit", str(crate), "--profile", "quick", "--dry-run"])

        err = capsys.readouterr().err
        assert code == 2
        assert "INTERNAL ERROR" not in err
        assert "veyra.toml" in err


class TestATypoInAToolListIsRefused:
    def test_an_unknown_include_id_stops_the_audit_before_evidence(
        self, crate, monkeypatch, capsys
    ):
        _stub_cli(monkeypatch, crate)

        code = main(["audit", str(crate), "--profile", "quick", "--include", "nope"])

        captured = capsys.readouterr()
        assert code == 2
        assert "nope" in captured.err
        assert "list-tools" in captured.err
        # Nothing ran and no verdict was written: a refusal leaves no evidence.
        assert captured.out == ""
        assert not (crate / ".veyra").exists()

    def test_an_unknown_exclude_id_stops_the_plan_too(self, crate, monkeypatch, capsys):
        """`plan` used to exit 0 here while the audit exited 2."""
        _stub_cli(monkeypatch, crate)

        code = main(["plan", str(crate), "--profile", "quick", "--exclude", "nope"])

        assert code == 2
        assert "nope" in capsys.readouterr().err


class TestTwoRunsNeverShareOneEvidenceTree:
    """Measured before this existed: two audits into one ``--evidence-dir``.

    Run A wrote ``tools/rustfmt/result.json``; run B, asking only for ``audit``, came
    back with both directories present while its own ``report.json`` named one tool.
    The tree therefore attributed to run B a check run B never made — and the default
    directory only counts seconds, so two runs in the same second collided the same
    way. A verdict's evidence has to describe the run that produced it.
    """

    def test_the_fresh_path_is_the_base_when_nothing_is_there(self, tmp_path):
        base = tmp_path / "evidence"

        assert unique_evidence_dir(base) == base

    def test_an_existing_path_gets_a_deterministic_suffix(self, tmp_path):
        base = tmp_path / "evidence"
        base.mkdir()

        assert unique_evidence_dir(base) == tmp_path / "evidence-2"

        (tmp_path / "evidence-2").mkdir()
        assert unique_evidence_dir(base) == tmp_path / "evidence-3"

    def test_a_second_audit_writes_beside_the_first_and_inherits_nothing(
        self, crate, monkeypatch, capsys
    ):
        _stub_cli(monkeypatch, crate)
        target = crate / "ev"
        argv = [
            "audit",
            str(crate),
            "--profile",
            "quick",
            "--include",
            "veyra-anti-theater",
            "--evidence-dir",
            str(target),
        ]

        main(argv)
        main(argv)
        capsys.readouterr()

        second = crate / "ev-2"
        assert target.is_dir() and second.is_dir()
        first_tools = {path.name for path in (target / "tools").iterdir()}
        second_tools = {path.name for path in (second / "tools").iterdir()}
        assert first_tools == {"veyra-anti-theater"}
        assert second_tools == first_tools
        first_report = target / "manifest.json"
        second_report = second / "manifest.json"
        assert set(json.loads(first_report.read_text())["results_status"]) == first_tools
        assert set(json.loads(second_report.read_text())["results_status"]) == second_tools


class TestTheVerdictNamesWhyARequiredToolDidNotRun:
    def test_excluded_tools_are_not_described_as_missing(self):
        decision = decide(
            [],
            fail_on="findings",
            required_tool_ids=["rustfmt"],
            excluded_required=["rustfmt"],
        )

        assert decision.decision == "INCONCLUSIVE"
        assert decision.exit_code == 2
        assert "--exclude" in decision.reason
        assert "missing or not executable" not in decision.reason

    def test_a_tool_that_is_really_absent_is_still_called_missing(self):
        decision = decide(
            [],
            fail_on="findings",
            required_tool_ids=["rustfmt"],
            excluded_required=[],
        )

        assert decision.exit_code == 2
        assert "missing or not executable" in decision.reason
