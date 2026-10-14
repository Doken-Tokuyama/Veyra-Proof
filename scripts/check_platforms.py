#!/usr/bin/env python3
"""Refuse a release whose suite has not passed on the *other* platforms.

The gate runs the suite on one machine. That machine is not the one that will
break: v0.4.3 was tagged here, published, and only *then* did CI come back red on
both Windows jobs — so the failure was discovered after the release it belonged
to, and the fix had to be shipped as 0.4.4. Nothing local can see that. The
evidence lives on the platform that failed.

So this step reads the evidence instead of producing it. For the exact commit
being tagged it asks GitHub for the CI run, and requires two things:

* every job of that run to have concluded ``success`` — a failed, cancelled or
  skipped job is not a pass, and a run still in flight is not evidence yet;
* every platform this project promises to support to be represented among the
  successful jobs. ``REQUIRED_PLATFORMS`` is that promise, and
  ``tests/test_check_platforms.py`` holds it against the matrix in
  ``.github/workflows/ci.yml``, so dropping Windows from the matrix fails the
  suite rather than quietly shrinking what a release proves.

A commit with no run at all is refused too, with the one instruction that fixes
it: push it, wait for CI, run the gate again. That order is the intended release
flow — the commit goes up, CI proves it on three platforms, then the tag is cut.

Exit 0 when the other platforms have passed for this commit, 1 when they have not
(or have not yet), 2 when this machine cannot ask GitHub at all (no ``gh``, no
credentials, no network). Exit 2 is not a pass: the gate treats any non-zero step
as a refusal, so an unverifiable release is never tagged — it is only ever left
unproven, which is what it is.

Usage::

    python3 scripts/check_platforms.py
    python3 scripts/check_platforms.py --commit <sha>
    python3 scripts/check_platforms.py --workflow CI --require windows-latest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The platforms a release claims to have passed on. The README's packaging table
# and the `Operating System` classifiers say the same thing; the CI matrix is what
# has to deliver it, and the suite asserts the matrix still covers this list.
REQUIRED_PLATFORMS = ("ubuntu-latest", "macos-latest", "windows-latest")

# The workflow that carries the platform matrix. `publish.yml` runs *after* a tag
# and cannot be the evidence for cutting one.
DEFAULT_WORKFLOW = "CI"

# Conclusions GitHub can report for a finished job. Anything that is not
# `success` leaves the platform unproven, which is why the check names the value
# it found rather than treating every non-success as "failed".
OK_CONCLUSION = "success"


@dataclass(frozen=True)
class Job:
    """One job of a workflow run, as GitHub reports it."""

    name: str
    status: str
    conclusion: str | None
    url: str = ""


@dataclass(frozen=True)
class Run:
    """One workflow run for a commit."""

    run_id: int
    status: str
    conclusion: str | None
    event: str
    branch: str
    url: str
    jobs: tuple[Job, ...] = ()


def gh(argv: list[str], root: Path) -> tuple[int, str, str]:
    """Run ``gh`` and return (status, stdout, stderr) without raising."""
    try:
        completed = subprocess.run(
            ["gh", *argv], cwd=root, capture_output=True, text=True, encoding="utf-8"
        )
    except FileNotFoundError:
        return 127, "", "gh is not installed"
    return completed.returncode, completed.stdout, completed.stderr


def repo_slug(root: Path) -> str | None:
    """``owner/name`` for this checkout, from gh or from the git remote."""
    status, out, _ = gh(
        ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], root
    )
    if status == 0 and out.strip():
        return out.strip()

    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", completed.stdout.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def resolve_commit(root: Path, ref: str) -> str | None:
    """The full 40-character SHA for ``ref``, or None.

    Full, not as given: GitHub's ``head_sha`` filter only matches a complete SHA,
    and a short one comes back as an empty list — a false "no CI run for this
    commit" for a commit whose run failed, which is the one wrong answer this
    check exists to prevent.
    """
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def runs_for(slug: str, sha: str, root: Path) -> tuple[int, list[dict], str]:
    """Every workflow run GitHub has for this commit."""
    status, out, err = gh(
        ["api", f"repos/{slug}/actions/runs?head_sha={sha}&per_page=100"], root
    )
    if status != 0:
        return status, [], err
    try:
        return 0, json.loads(out).get("workflow_runs", []), ""
    except ValueError as error:
        return 1, [], f"GitHub returned something that is not JSON: {error}"


def jobs_for(slug: str, run_id: int, root: Path) -> tuple[int, list[dict], str]:
    """The jobs of one run, with their conclusions."""
    status, out, err = gh(["api", f"repos/{slug}/actions/runs/{run_id}/jobs?per_page=100"], root)
    if status != 0:
        return status, [], err
    try:
        return 0, json.loads(out).get("jobs", []), ""
    except ValueError as error:
        return 1, [], f"GitHub returned something that is not JSON: {error}"


def pick_run(runs: list[dict], workflow: str) -> dict | None:
    """The newest run of the required workflow, or None.

    Newest rather than first: a commit can carry several runs of the same workflow
    (a re-run, or a push followed by a re-run of the failed jobs), and the only one
    worth reading is the latest attempt.
    """
    matching = [run for run in runs if run.get("name") == workflow]
    if not matching:
        return None
    return max(matching, key=lambda run: run.get("run_number", 0))


def classify(
    sha: str,
    workflow: str,
    run: dict | None,
    jobs: list[dict],
    required: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Return (problems, confirmed) for one commit's evidence.

    Pure, so every refusal below can be driven in a test without a network: a
    check nobody can make fail is decoration, and this file is a check.
    """
    problems: list[str] = []
    confirmed: list[str] = []

    if run is None:
        problems.append(
            f"no {workflow} run exists for {sha[:12]}: push the commit, let CI finish, "
            "then run the gate again"
        )
        return problems, confirmed

    label = f"{workflow} run {run.get('run_number', '?')} ({run.get('event', '?')})"
    url = run.get("html_url", "")

    if run.get("status") != "completed":
        problems.append(
            f"{label} for {sha[:12]} is still {run.get('status', 'unknown')}: wait for it "
            "to finish, then run the gate again"
        )
        return problems, confirmed

    if run.get("conclusion") != OK_CONCLUSION:
        problems.append(
            f"{label} for {sha[:12]} concluded {run.get('conclusion')!r} ({url})"
        )

    if not jobs:
        problems.append(f"{label} reports no jobs, so nothing was actually run")

    successful = [job for job in jobs if job.get("conclusion") == OK_CONCLUSION]
    for job in jobs:
        name = job.get("name", "?")
        conclusion = job.get("conclusion")
        if conclusion == OK_CONCLUSION:
            confirmed.append(f"{name}  success")
        else:
            problems.append(
                f"job {name!r} concluded {conclusion!r} on {run.get('head_branch', '?')} "
                f"({job.get('html_url', url)})"
            )

    # The promise, checked against what actually ran. A job that was cancelled or
    # skipped leaves the platform unproven even when the run as a whole is green.
    for platform in required:
        on_platform = [job for job in successful if platform in job.get("name", "")]
        if not on_platform:
            problems.append(
                f"no successful job on {platform}: this release would be the first time "
                "it runs there"
            )
        else:
            versions = sorted(
                {
                    match.group(0)
                    for job in on_platform
                    for match in re.finditer(r"py3\.\d+", job.get("name", ""))
                }
            )
            tail = f" ({', '.join(versions)})" if versions else ""
            confirmed.append(f"{platform}: {len(on_platform)} job(s) success{tail}")

    return problems, confirmed


def write_report(path: Path, facts: dict, ok: bool) -> None:
    """State the measurement in machine-readable form, for the release notes.

    The gate records what this step *measured* rather than scraping the paragraphs
    it printed, so a number in a release note can only ever be one that was read
    out of an artefact. Written through a temp file, so a reader never sees half
    of one. A report this cannot write is not fatal: it costs a release-note row.
    """
    payload = {"tool": "check_platforms", "ok": ok, "facts": facts}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as error:
        print(f"note: could not write the report to {path}: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check that the CI run for a commit passed on every platform this "
            "project supports, so a release is not the first place an untested "
            "platform reports in."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Checkout whose commit and remote are used (default: parent of this script).",
    )
    parser.add_argument("--commit", default=None, help="Commit to check (default: HEAD).")
    parser.add_argument("--repo", default=None, help="owner/name (default: this checkout's).")
    parser.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW,
        help=f"Workflow that carries the platform matrix (default: {DEFAULT_WORKFLOW}).",
    )
    parser.add_argument(
        "--require",
        default=",".join(REQUIRED_PLATFORMS),
        help="Comma-separated platforms that must have a successful job.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write what was measured, as JSON, to this path (used by the gate).",
    )
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    required = tuple(item.strip() for item in args.require.split(",") if item.strip())

    sha = resolve_commit(root, args.commit or "HEAD")
    if not sha:
        target = args.commit or "HEAD"
        sys.stdout.flush()
        print(
            f"cannot resolve {target!r} to a commit in {root}: nothing to check",
            file=sys.stderr,
        )
        return 2

    slug = args.repo or repo_slug(root)
    if not slug:
        sys.stdout.flush()
        print(
            "cannot tell which repository this is: no gh login and no origin remote",
            file=sys.stderr,
        )
        return 2

    print(f"repo       {slug}")
    print(f"commit     {sha}")
    print(f"workflow   {args.workflow}")
    print(f"required   {', '.join(required)}")
    print(flush=True)

    status, runs, err = runs_for(slug, sha, root)
    if status != 0:
        sys.stdout.flush()
        print(f"  NOT RUN  gh could not read the runs for {sha[:12]}: {err.strip()}", file=sys.stderr)
        print("           this is not a pass: no platform has been proven for this commit", file=sys.stderr)
        return 2

    run = pick_run(runs, args.workflow)
    jobs: list[dict] = []
    if run is not None:
        status, jobs, err = jobs_for(slug, run["id"], root)
        if status != 0:
            sys.stdout.flush()
            print(f"  NOT RUN  gh could not read the jobs of run {run.get('id')}: {err.strip()}", file=sys.stderr)
            return 2

    problems, confirmed = classify(sha, args.workflow, run, jobs, required)

    for line in confirmed:
        print(f"  confirmed  {line}")

    if args.report is not None:
        successful = [job for job in jobs if job.get("conclusion") == OK_CONCLUSION]
        facts = {
            "commit": sha,
            "workflow": args.workflow,
            "required": list(required),
            "jobs": len(jobs),
            # Per platform, the python versions whose job passed: the promise and
            # what actually delivered it. Empty lists are absent platforms.
            "platforms": {
                platform: sorted(
                    {
                        match.group(0)
                        for job in successful
                        if platform in job.get("name", "")
                        for match in re.finditer(r"py3\.\d+", job.get("name", ""))
                    }
                )
                for platform in required
            },
            "run": run.get("html_url", "") if run else "",
        }
        write_report(args.report, facts, ok=not problems)

    if problems:
        print()
        for problem in problems:
            print(f"  FAIL  {problem}")
        print()
        print("the other platforms have not passed for this commit, so the tag was refused")
        return 1

    print()
    print(f"  {args.workflow} passed on {', '.join(required)} for {sha[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
