#!/usr/bin/env python3
"""Run the auditor for real, then check that the evidence backs the verdict.

The unit suite mocks ``subprocess.run``. That proves the plumbing is wired up
and proves nothing about whether a real cargo invocation works. This script is
the opposite: it runs the CLI end to end, then refuses to pass unless the run
left on disk the evidence it claims to have produced.

    python3 scripts/ci_assert_evidence.py tests/fixtures/demo_crate
    python3 scripts/ci_assert_evidence.py . --profile standard

The assertion that carries the weight is the cross-check in :func:`verify`. Every
tool reported ``OK`` must have a log recorded on disk and a per-tool
``result.json`` that agrees with the summary on both status and exit code. Two
independently written artifacts have to tell the same story. That is the thesis
of this project, expressed as a check that can fail.

A successful ``cargo fmt --check`` on already-formatted code prints nothing, so
an empty log is a silent success and is not treated as a defect. The artifact
has to exist; it does not have to say anything.

Exit 0 when the run produced usable evidence, 1 when it did not. A verdict of
"problems found" (auditor exit 1) is a successful audit, not a failed one, so it
is accepted here as well; exits 2 and 3 mean missing prerequisites or an
internal error, and do fail.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Statuses the auditor is allowed to report. Anything else means the report and
# the tool disagree about the vocabulary, which is itself a defect.
KNOWN_STATUSES = {
    "INSTALLED",
    "MISSING",
    "INSTALLING",
    "SKIPPED",
    "ERROR",
    "OK",
    "FINDINGS",
    "INCONCLUSIVE",
}

# The auditor's own exit codes. A verdict is 0 or 1; 2 means missing
# prerequisites and 3 an internal error, and neither is a usable run.
VERDICT_EXITS = {0, 1}

CORE_ARTIFACTS = ("report.json", "report.md", "manifest.json")


def evidence_root(target: Path) -> Path:
    return target / ".veyra" / "evidence"


def newest_evidence(target: Path, before: set[Path]) -> Path | None:
    """The run that just happened, not merely the latest one on disk."""
    root = evidence_root(target)
    if not root.is_dir():
        return None
    fresh = [p for p in root.iterdir() if p.is_dir() and p not in before]
    if fresh:
        return max(fresh, key=lambda p: p.name)
    # A rerun inside the same UTC second would land on an existing directory.
    existing = [p for p in root.iterdir() if p.is_dir()]
    return max(existing, key=lambda p: p.name) if existing else None


def run_audit(target: Path, profile: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "veyra_proof",
            "audit",
            str(target),
            "--profile",
            profile,
        ],
        capture_output=True,
        text=True,
    )


def verify(
    directory: Path, process_exit_code: int | None = None
) -> tuple[list[str], list[str]]:
    """Return (problems, confirmed) for an evidence directory.

    An empty ``problems`` means the evidence holds up. Split out from the runner
    so faults can be injected in tests: a check nobody can make fail is
    decoration, and this file is a check.
    """
    problems: list[str] = []

    for name in CORE_ARTIFACTS:
        path = directory / name
        if not path.is_file():
            problems.append(f"{name} is missing")
        elif path.stat().st_size == 0:
            problems.append(f"{name} is empty")

    if problems:
        return problems, []

    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    results = report.get("results", [])
    required = set(report.get("required_tool_ids", []))

    if not results:
        problems.append("report.json lists no tool results")
        return problems, []

    confirmed: list[str] = []
    for entry in results:
        tool_id = entry.get("tool_id", "?")
        status = entry.get("status", "?")
        code = entry.get("exit_code")
        fault: str | None = None

        if status not in KNOWN_STATUSES:
            fault = f"status {status!r} is outside the documented vocabulary"
        elif status == "OK":
            log = entry.get("stdout_path")
            if code is None:
                fault = "reported OK without a recorded exit code"
            elif not log or not Path(log).is_file():
                fault = "reported OK without a log on disk"

        if fault is None:
            record = directory / "tools" / tool_id / "result.json"
            if not record.is_file():
                fault = "has no per-tool record to confirm the status"
            else:
                own = json.loads(record.read_text(encoding="utf-8"))
                if own.get("status") != status or own.get("exit_code") != code:
                    fault = (
                        f"summary claims {status}/{code}, its own record says "
                        f"{own.get('status')}/{own.get('exit_code')}"
                    )

        if fault:
            problems.append(f"{tool_id}: {fault}")
        else:
            confirmed.append(f"{tool_id} {status} exit {code}")

    absent = required - {e.get("tool_id") for e in results}
    if absent:
        problems.append(f"required tools absent from the report: {sorted(absent)}")

    decision = report.get("decision", {})
    if process_exit_code is not None and decision.get("exit_code") != process_exit_code:
        problems.append(
            f"report claims exit {decision.get('exit_code')} "
            f"but the process returned {process_exit_code}"
        )

    return problems, confirmed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a real audit and verify the evidence it leaves behind."
    )
    parser.add_argument("target", nargs="?", default=".", help="Cargo workspace root")
    parser.add_argument("--profile", default="quick", help="audit profile to run")
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    if not (target / "Cargo.toml").is_file():
        print(f"no Cargo.toml in {target}", file=sys.stderr)
        return 1

    root = evidence_root(target)
    before = set(root.iterdir()) if root.is_dir() else set()

    print(f"running  veyra audit {target} --profile {args.profile}")
    completed = run_audit(target, args.profile)
    print(f"  auditor exit code: {completed.returncode}")

    if completed.returncode not in VERDICT_EXITS:
        print(
            f"  FAIL  exit {completed.returncode} is not a verdict "
            "(2 = missing prerequisites, 3 = internal error)"
        )
        print(completed.stdout[-2000:])
        print(completed.stderr[-2000:], file=sys.stderr)
        return 1

    directory = newest_evidence(target, before)
    if directory is None:
        print("  FAIL  the run wrote no evidence directory")
        return 1
    print(f"  evidence  {directory}")

    problems, confirmed = verify(directory, completed.returncode)
    for line in confirmed:
        print(f"  confirmed  {line}")

    if problems:
        print()
        for problem in problems:
            print(f"  FAIL  {problem}")
        print(f"\n{len(problems)} problem(s): the run did not back up its own claim")
        return 1

    print("\nevidence is consistent with the verdict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
