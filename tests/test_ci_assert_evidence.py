"""The CI evidence checker has to be able to fail.

A verification script that only ever prints "consistent" is worse than no
check at all: it manufactures the exact false confidence this project exists
to refuse. Every assertion in ``scripts/ci_assert_evidence.py`` is exercised
here against a synthetic evidence directory with a fault deliberately injected.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci_assert_evidence.py"


def _load():
    spec = importlib.util.spec_from_file_location("ci_assert_evidence", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load()


def _evidence(tmp_path: Path, *, status: str = "OK", exit_code: int = 0) -> Path:
    """A minimal but genuinely well-formed evidence directory."""
    directory = tmp_path / "evidence" / "20260101T000000Z"
    tools = directory / "tools" / "rustfmt"
    tools.mkdir(parents=True)

    log = tools / "stdout.log"
    log.write_text("", encoding="utf-8")  # a silent success writes no output

    record = {"tool_id": "rustfmt", "status": status, "exit_code": exit_code}
    (tools / "result.json").write_text(json.dumps(record), encoding="utf-8")

    report = {
        "decision": {"decision": "ACCEPTED", "exit_code": 0},
        "required_tool_ids": ["rustfmt"],
        "results": [
            {
                "tool_id": "rustfmt",
                "status": status,
                "exit_code": exit_code,
                "stdout_path": str(log),
            }
        ],
    }
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (directory / "report.md").write_text("# Veyra Proof Report\n", encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
    return directory


def test_well_formed_evidence_passes(checker, tmp_path) -> None:
    """A silent success with an empty log is a pass, not a defect."""
    problems, confirmed = checker.verify(_evidence(tmp_path), 0)
    assert problems == []
    assert confirmed == ["rustfmt OK exit 0"]


def test_a_silently_passing_tool_still_needs_its_log_on_disk(checker, tmp_path) -> None:
    directory = _evidence(tmp_path)
    (directory / "tools" / "rustfmt" / "stdout.log").unlink()

    problems, _ = checker.verify(directory, 0)
    assert any("without a log on disk" in p for p in problems)


def test_summary_contradicting_its_own_record_is_caught(checker, tmp_path) -> None:
    """The core cross-check: two artifacts, one story, or it is a failure."""
    directory = _evidence(tmp_path)
    (directory / "tools" / "rustfmt" / "result.json").write_text(
        json.dumps({"tool_id": "rustfmt", "status": "ERROR", "exit_code": 101}),
        encoding="utf-8",
    )

    problems, _ = checker.verify(directory, 0)
    assert any("its own record says" in p for p in problems)


def test_ok_without_an_exit_code_is_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path, exit_code=None)
    (directory / "tools" / "rustfmt" / "result.json").write_text(
        json.dumps({"tool_id": "rustfmt", "status": "OK", "exit_code": None}),
        encoding="utf-8",
    )

    problems, _ = checker.verify(directory, 0)
    assert any("without a recorded exit code" in p for p in problems)


def test_a_tool_with_no_per_tool_record_is_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path)
    (directory / "tools" / "rustfmt" / "result.json").unlink()

    problems, _ = checker.verify(directory, 0)
    assert any("no per-tool record" in p for p in problems)


def test_status_outside_the_vocabulary_is_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path, status="PROBABLY_FINE")
    (directory / "tools" / "rustfmt" / "result.json").write_text(
        json.dumps({"tool_id": "rustfmt", "status": "PROBABLY_FINE", "exit_code": 0}),
        encoding="utf-8",
    )

    problems, _ = checker.verify(directory, 0)
    assert any("outside the documented vocabulary" in p for p in problems)


def test_skipped_tool_is_not_demanded_a_log(checker, tmp_path) -> None:
    """SKIPPED is an honest outcome; only OK is a claim that needs backing."""
    directory = _evidence(tmp_path, status="SKIPPED", exit_code=None)
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    report["results"][0]["stdout_path"] = "/nonexistent/nowhere.log"
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (directory / "tools" / "rustfmt" / "result.json").write_text(
        json.dumps({"tool_id": "rustfmt", "status": "SKIPPED", "exit_code": None}),
        encoding="utf-8",
    )

    problems, _ = checker.verify(directory, 0)
    assert problems == []


def test_a_required_tool_missing_from_the_report_is_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path)
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    report["required_tool_ids"] = ["rustfmt", "clippy"]
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")

    problems, _ = checker.verify(directory, 0)
    assert any("required tools absent" in p for p in problems)


def test_a_report_claiming_a_different_exit_code_is_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path)

    problems, _ = checker.verify(directory, 1)
    assert any("but the process returned 1" in p for p in problems)


def test_missing_core_artifacts_are_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path)
    (directory / "manifest.json").unlink()

    problems, confirmed = checker.verify(directory, 0)
    assert problems == ["manifest.json is missing"]
    assert confirmed == []


def test_empty_core_artifact_is_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path)
    (directory / "report.md").write_text("", encoding="utf-8")

    problems, _ = checker.verify(directory, 0)
    assert problems == ["report.md is empty"]


def _e2e_evidence(tmp_path: Path, *, log_text: str, declared_scenarios: int) -> tuple[Path, Path]:
    """Evidence for the end-to-end tool plus the target's veyra.toml."""
    target = tmp_path / "target-crate"
    (target / ".veyra" / "evidence" / "20260101T000000Z" / "tools" / "veyra-e2e").mkdir(
        parents=True
    )
    if declared_scenarios:
        target.mkdir(parents=True, exist_ok=True)
        declarations = "\n".join(
            f'[[e2e.scenarios]]\nname = "s{i}"\nargv = ["run"]\n'
            for i in range(declared_scenarios)
        )
        (target / "veyra.toml").write_text(declarations, encoding="utf-8")

    directory = target / ".veyra" / "evidence" / "20260101T000000Z"
    tools = directory / "tools" / "veyra-e2e"
    log = tools / "stdout.log"
    log.write_text(log_text, encoding="utf-8")
    record = {
        "tool_id": "veyra-e2e",
        "status": "OK",
        "exit_code": 0,
        "environment": {"evidence_kind": "e2e"},
        "stdout_path": str(log),
    }
    (tools / "result.json").write_text(json.dumps(record), encoding="utf-8")
    report = {
        "decision": {"decision": "ACCEPTED", "exit_code": 0},
        "required_tool_ids": ["veyra-e2e"],
        "results": [
            {
                "tool_id": "veyra-e2e",
                "status": "OK",
                "exit_code": 0,
                "stdout_path": str(log),
            }
        ],
    }
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (directory / "report.md").write_text("# Veyra Proof Report\n", encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
    return directory, target


def test_an_end_to_end_run_that_observed_every_scenario_passes(checker, tmp_path) -> None:
    log = (
        "binary: /tmp/demo (sha256=abc)\n"
        "SCENARIO name='a' argv=['add', '1', '2'] observed_exit=0 verdict=PASS\n"
        "SCENARIO name='b' argv=['divide', '1', '0'] observed_exit=2 verdict=PASS\n"
    )
    directory, target = _e2e_evidence(tmp_path, log_text=log, declared_scenarios=2)

    problems, confirmed = checker.verify(directory, 0, target=target)

    assert problems == []
    assert confirmed == ["veyra-e2e OK exit 0"]


def test_an_end_to_end_ok_with_no_observed_scenario_is_caught(checker, tmp_path) -> None:
    """The check this guards: a green claim with nothing observed behind it."""
    directory, target = _e2e_evidence(
        tmp_path, log_text="binary: /tmp/demo (sha256=abc)\n", declared_scenarios=1
    )

    problems, _ = checker.verify(directory, 0, target=target)

    assert any("without a single observed scenario" in p for p in problems)


def test_an_end_to_end_run_that_skipped_a_declared_scenario_is_caught(checker, tmp_path) -> None:
    log = "SCENARIO name='a' argv=['add'] observed_exit=0 verdict=PASS\n"
    directory, target = _e2e_evidence(tmp_path, log_text=log, declared_scenarios=3)

    problems, _ = checker.verify(directory, 0, target=target)

    assert any("observed 1 scenario(s) but veyra.toml declares 3" in p for p in problems)


def test_an_end_to_end_scenario_line_without_an_observed_exit_is_caught(checker, tmp_path) -> None:
    directory, target = _e2e_evidence(
        tmp_path,
        log_text="SCENARIO name='a' argv=['add'] verdict=PASS\n",
        declared_scenarios=1,
    )

    problems, _ = checker.verify(directory, 0, target=target)

    assert any("records no observed exit code" in p for p in problems)


def test_a_report_with_no_results_is_caught(checker, tmp_path) -> None:
    directory = _evidence(tmp_path)
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    report["results"] = []
    report["required_tool_ids"] = []
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")

    problems, _ = checker.verify(directory, 0)
    assert problems == ["report.json lists no tool results"]
