"""Live proof that the auditor rejects a defective workspace.

The unit suite can show that a blocker finding maps to REJECTED. It cannot show
that a real ``todo!()`` in a real crate survives discovery, compilation and the
anti-theater scan to produce that blocker on disk. This runs the real CLI, with
the real cargo toolchain, against a fixture whose defects are deliberate.

A suite that only ever audits a clean fixture has proven the happy path and
nothing about the verdict the tool exists to deliver.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "broken_crate"

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None,
    reason="cargo is required to build the broken fixture",
)


def _audit(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "veyra_proof",
            "audit",
            str(target),
            "--profile",
            "quick",
            "--include",
            "cargo-test,veyra-anti-theater",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
    )


@pytest.fixture()
def fixture_copy(tmp_path: Path) -> Path:
    target = tmp_path / "broken_crate"
    shutil.copytree(
        FIXTURE, target, ignore=shutil.ignore_patterns("target", ".veyra", "__pycache__")
    )
    return target


def _latest_evidence(target: Path) -> Path:
    root = target / ".veyra" / "evidence"
    directories = [p for p in root.iterdir() if p.is_dir()]
    assert directories, "the audit wrote no evidence directory"
    return max(directories, key=lambda p: p.name)


def test_a_todo_in_production_is_rejected_by_a_real_audit(fixture_copy: Path) -> None:
    completed = _audit(fixture_copy)

    assert completed.returncode == 1, completed.stdout[-4000:] + completed.stderr[-2000:]
    report = json.loads(completed.stdout)
    assert report["decision"]["decision"] == "REJECTED"
    assert set(report["required_tool_ids"]) == {"cargo-test", "veyra-anti-theater"}

    results = {entry["tool_id"]: entry for entry in report["results"]}

    anti = results["veyra-anti-theater"]
    assert anti["status"] == "FINDINGS", anti["summary"]
    todo = next(f for f in anti["findings"] if f["rule"] == "VT001")
    assert todo["severity"] == "blocker"
    assert "todo" in todo["explanation"].lower()

    tests = results["cargo-test"]
    assert tests["status"] == "FINDINGS", tests["summary"]
    assert tests["exit_code"] == 101
    failures = [f for f in tests["findings"] if f["rule"] == "test-failure"]
    assert failures, "the failing test was not reported as a test-failure finding"
    assert any("answer_is_implemented" in (f["excerpt"] or "") for f in failures)

    # The verdict is only usable if the evidence on disk backs it.
    evidence = _latest_evidence(fixture_copy)
    assert (evidence / "report.json").is_file()
    assert (evidence / "manifest.json").is_file()
    for tool_id in ("veyra-anti-theater", "cargo-test"):
        entry = results[tool_id]
        for name, digest in entry["artifact_hashes"].items():
            path = evidence / "tools" / tool_id / name
            if path.is_file():
                assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
