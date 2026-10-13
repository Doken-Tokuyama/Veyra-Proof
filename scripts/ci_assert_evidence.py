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

Every log the auditor writes is pinned by its sha256 in the report, so a log
edited or deleted after the run fails this check instead of passing quietly. A
few tools also pin artifacts that live elsewhere and name them with synthetic
keys (``a:<crate>`` inside a build directory); those are verified only when the
file can be located from the report alone.

Exit 0 when the run produced usable evidence, 1 when it did not. A verdict of
"problems found" (auditor exit 1) is a successful audit, not a failed one, so it
is accepted here as well; exits 2 and 3 mean missing prerequisites or an
internal error, and do fail.
"""

from __future__ import annotations

import argparse
import hashlib
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

# The end-to-end tool claims behavior, not merely a successful run, so its log
# has to carry the values each scenario observed. "OK" with nothing observed
# would be the exact fake confidence this project refuses to manufacture.
E2E_TOOL_ID = "veyra-e2e"

CORE_ARTIFACTS = ("report.json", "report.md", "manifest.json")

# The files a tool writes as evidence for its own verdict. These have to exist
# and match their recorded digest; an absent log is a claim nobody can check.
EVIDENCE_LOG_NAMES = ("stdout.log", "stderr.log", "skip.txt")


def _is_digest(value: object) -> bool:
    """A sha256 hex digest. A few tools store a bare path in this field instead,
    which is not a digest and is left alone rather than guessed at."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_path(directory: Path, tool_id: str, name: str) -> Path:
    """Where a pinned artifact should be. A bare file name is evidence the tool
    wrote in its own directory; a path names something built elsewhere."""
    if "/" in name or "\\" in name:
        candidate = Path(name)
        return candidate if candidate.is_absolute() else directory / candidate
    return directory / "tools" / tool_id / name


def evidence_root(target: Path) -> Path:
    return target / ".veyra" / "evidence"


def declared_e2e_scenarios(target: Path | None) -> int | None:
    """Scenarios declared in veyra.toml, counted without a TOML dependency."""
    if target is None:
        return None
    path = target / "veyra.toml"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").count("[[e2e.scenarios]]")


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
    directory: Path,
    process_exit_code: int | None = None,
    target: Path | None = None,
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
        elif status in {"FINDINGS", "ERROR"}:
            # A finding was parsed out of something the tool read, so both
            # captures have to hold it. Empty logs here mean the verdict is
            # unsupported — the exact "all clear with no evidence" shape this
            # check exists to refuse. (`cargo fmt --check` on clean code is
            # legitimately silent, which is why OK is not checked this way.)
            logs = [
                Path(entry.get(field))
                for field in ("stdout_path", "stderr_path")
                if entry.get(field)
            ]
            present = [path for path in logs if path.is_file()]
            if not logs:
                fault = f"reported {status} without any captured output"
            elif present and not any(path.stat().st_size > 0 for path in present):
                fault = f"reported {status} but every captured log is empty"
            # A log that is gone entirely is left to the digest check below,
            # which names the missing file rather than the empty capture.

        own = None
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

        # Every recorded digest has to still describe the file it pinned, so an
        # edited or deleted log fails here instead of passing quietly.
        if fault is None and own is not None:
            declared = entry.get("artifact_hashes") or {}
            recorded = own.get("artifact_hashes") or {}
            for name, digest in sorted(declared.items()):
                if not _is_digest(digest):
                    continue
                if recorded.get(name) != digest:
                    fault = f"report and its own record disagree about the digest of {name}"
                    break
                path = artifact_path(directory, tool_id, name)
                if name in EVIDENCE_LOG_NAMES and not path.is_file():
                    fault = f"records a digest for {name}, which is not on disk"
                    break
                if path.is_file() and _sha256(path) != digest:
                    fault = f"{name} does not match its recorded digest"
                    break

        # An OK claim has to pin the log it points at, not merely leave it there.
        if fault is None and own is not None and status == "OK":
            declared = entry.get("artifact_hashes") or {}
            for field in ("stdout_path", "stderr_path"):
                raw = entry.get(field)
                if not raw:
                    continue
                path = Path(raw)
                if path.is_file() and not _is_digest(declared.get(path.name)):
                    fault = f"{path.name} is on disk but its digest was not recorded"
                    break

        # A tool that verifies end-to-end behavior has to show what it observed.
        if (
            fault is None
            and own is not None
            and tool_id == E2E_TOOL_ID
            and status == "OK"
            and (own.get("environment") or {}).get("evidence_kind") == "e2e"
        ):
            log = own.get("stdout_path")
            text = Path(log).read_text(encoding="utf-8") if log and Path(log).is_file() else ""
            observed = [line for line in text.splitlines() if line.startswith("SCENARIO ")]
            if not observed:
                fault = "reported OK without a single observed scenario in its log"
            elif any("observed_exit=" not in line for line in observed):
                fault = "reported OK with a scenario line that records no observed exit code"
            else:
                declared = declared_e2e_scenarios(target)
                if declared is not None and len(observed) != declared:
                    fault = (
                        f"observed {len(observed)} scenario(s) but veyra.toml declares "
                        f"{declared}"
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

    problems, confirmed = verify(directory, completed.returncode, target=target)
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
