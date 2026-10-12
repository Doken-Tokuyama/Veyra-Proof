"""Live proof that the end-to-end tool actually verifies behavior.

The unit suite mocks the runner; this file does not. It copies the demo fixture
into a temporary directory — an environment as isolated as a test can be without
touching anyone's real workspace — lets the real cargo build it, and checks the
observable values the run left on disk: the verdict, the per-scenario exit codes,
the captured output, and the hash of the binary that produced it.

The second test exists to prove the check can fail. A suite whose negative case
is never exercised manufactures the exact false confidence this project refuses.
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

from veyra_proof.tools.e2e import _excerpt

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "demo_crate"

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None,
    reason="cargo is required to build the demo fixture",
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
            "veyra-e2e",
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
    target = tmp_path / "demo_crate"
    shutil.copytree(
        FIXTURE, target, ignore=shutil.ignore_patterns("target", ".veyra", "__pycache__")
    )
    return target


def _latest_evidence(target: Path) -> Path:
    root = target / ".veyra" / "evidence"
    directories = [p for p in root.iterdir() if p.is_dir()]
    assert directories, "the audit wrote no evidence directory"
    return max(directories, key=lambda p: p.name)


def test_the_demo_binary_is_verified_end_to_end(fixture_copy: Path) -> None:
    completed = _audit(fixture_copy)

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    report = json.loads(completed.stdout)
    assert report["decision"]["decision"] == "ACCEPTED"
    assert "veyra-e2e" in report["required_tool_ids"], (
        "declared scenarios must make the end-to-end check a requirement"
    )

    entry = next(r for r in report["results"] if r["tool_id"] == "veyra-e2e")
    assert entry["status"] == "OK", entry["summary"]

    evidence = _latest_evidence(fixture_copy)
    log = (evidence / "tools" / "veyra-e2e" / "stdout.log").read_text(encoding="utf-8")
    scenarios = [line for line in log.splitlines() if line.startswith("SCENARIO ")]
    assert len(scenarios) == 5

    # Observable results, not "it started": exit codes and printed output.
    observed_codes = {}
    for line in scenarios:
        argv = line.split(" argv=", 1)[1].split(" observed_exit=")[0]
        code = line.split("observed_exit=", 1)[1].split(" ")[0]
        observed_codes[argv] = code

    assert observed_codes["['add', '1', '2']"] == "0"
    assert observed_codes["['divide', '1', '0']"] == "2"
    assert observed_codes["['add', '2147483647', '1']"] == "2"
    assert observed_codes["['frobnicate']"] == "2"
    assert observed_codes["['--help']"] == "0"

    # The failure paths explain themselves on stderr, and the evidence keeps it.
    assert "error: division by zero" in log
    assert "error: arithmetic overflow" in log
    assert "error: unknown command 'frobnicate'" in log
    assert f"observed_stdout: '{_excerpt('3' + chr(10))}'" in log

    # The binary that produced this behavior is pinned by hash, and the hash
    # still matches the file cargo built.
    hashes = entry["artifact_hashes"]
    binary_keys = [key for key in hashes if "/" in key]
    assert len(binary_keys) == 1, f"expected exactly one pinned binary, got {sorted(hashes)}"
    binary = Path(binary_keys[0])
    assert binary.is_file()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == hashes[binary_keys[0]]

    # The logs the verdict rests on are pinned too, so neither can be swapped
    # for a friendlier one after the run.
    for name in ("stdout.log", "stderr.log"):
        path = evidence / "tools" / "veyra-e2e" / name
        assert hashes[name] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_wrong_expectation_is_rejected_with_the_value_observed(fixture_copy: Path) -> None:
    """Tamper with the declaration and the audit must refuse it."""
    config = fixture_copy / "veyra.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "expect_stdout_regex = '^3\\r?\\n?$'", "expect_stdout_regex = '^4$'"
        ),
        encoding="utf-8",
    )

    completed = _audit(fixture_copy)

    assert completed.returncode == 1, completed.stdout[-4000:]
    report = json.loads(completed.stdout)
    entry = next(r for r in report["results"] if r["tool_id"] == "veyra-e2e")
    assert entry["status"] == "FINDINGS"
    findings = [f["rule"] for f in entry["findings"]]
    assert "E2E02" in findings
    mismatch = next(f for f in entry["findings"] if f["rule"] == "E2E02")
    assert f"observed {_excerpt('3' + chr(10))!r}" in mismatch["explanation"], (
        "the finding has to quote what was actually observed, not just say it differed"
    )
