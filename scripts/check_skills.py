#!/usr/bin/env python3
"""One command that checks every copy of the Veyra skill.

Three copies exist, and they fail differently, which is why this was two
commands a maintainer had to remember separately:

* the **canonical** skill in this repo, under ``.cursor/skills/veyra-proof/`` —
  the source every other copy is compared against;
* the **harness** copies ``sync_skill.py`` fans out to every agent harness;
* the **account** copy Freebuff Desktop keeps, which belongs to that app and is
  only ever reported.

This runs both checks, prints both reports, and states one verdict. The policy is
the one CI applies, for the reason CI applies it:

* a fan-out copy that drifted is repairable here, so it **fails** — run
  ``sync_skill.py``;
* the account copy is a machine-local cache this repository cannot repair, so it
  **warns and passes**. Failing would mean a contributor whose Cloud cache is
  stale could never get a green check, which is how a check gets ignored;
* a sub-check that cannot run at all (exit 2, e.g. no canonical skill at
  ``--root``) **fails**, because that is a defect in the tooling rather than a
  copy that is out of date.

Both checks therefore always run and always report. Nothing is hidden behind an
early exit.

Usage::

    python3 scripts/check_skills.py
    python3 scripts/check_skills.py --root /path/to/checkout
    python3 scripts/check_skills.py --home /tmp/fake-home --store /tmp/fake-store

Exit 0 when nothing repairable drifted, 1 when the harness copies drifted, 2
when a check could not run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
FANOUT = SCRIPTS / "sync_skill.py"
ACCOUNT = SCRIPTS / "check_account_skill.py"

FIXABLE = "python3 scripts/sync_skill.py"


@dataclass(frozen=True)
class Outcome:
    """One check's exit status and the output it produced."""

    status: int
    output: str

    @property
    def could_not_run(self) -> bool:
        """2 or worse means the checker itself is broken, not a stale copy."""
        return self.status >= 2

    @property
    def drifted(self) -> bool:
        return self.status == 1


def run(script: Path, args: list[str]) -> Outcome:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
    )
    return Outcome(completed.returncode, (completed.stdout + completed.stderr).rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check the canonical skill, the harness fan-out and the Freebuff "
            "Desktop account copy in one pass."
        )
    )
    parser.add_argument("--root", type=Path, default=None, help="Checkout to check.")
    parser.add_argument("--home", type=Path, default=None, help="Home for the fan-out.")
    parser.add_argument("--store", type=Path, default=None, help="Account store to inspect.")
    args = parser.parse_args(argv)

    fanout_args = ["--check"]
    account_args: list[str] = []
    if args.root is not None:
        fanout_args += ["--root", str(args.root)]
        account_args += ["--root", str(args.root)]
    if args.home is not None:
        fanout_args += ["--home", str(args.home)]
    if args.store is not None:
        account_args += ["--store", str(args.store)]

    fanout = run(FANOUT, fanout_args)
    account = run(ACCOUNT, account_args)

    print("=== harness copies")
    print(f"$ python3 scripts/sync_skill.py {' '.join(fanout_args)}")
    print(fanout.output)
    print()
    print("=== account copy")
    print(f"$ python3 scripts/check_account_skill.py {' '.join(account_args)}")
    print(account.output)
    print()
    print("=== verdict")

    broken = [
        name
        for name, outcome in (("sync_skill.py", fanout), ("check_account_skill.py", account))
        if outcome.could_not_run
    ]
    if broken:
        print(f"  FAIL  {', '.join(broken)} could not run; see the report above.")
        print("        A checker that cannot run is a defect here, not a stale copy.")
        return 2

    if fanout.drifted:
        print("  FAIL  the harness copies no longer match the canonical skill.")
        print(f"        Repair: {FIXABLE}")
        if account.drifted:
            print("  WARN  the account copy lags too, and this command cannot repair it.")
            print("        Remove the synced entry in the app's skill manager.")
        return 1

    print("  PASS  every harness copy matches the canonical skill.")
    if account.drifted:
        print()
        print("  WARN  the Freebuff Desktop account copy is older than the canonical")
        print("        skill. It is a machine-local cache of the Cloud snapshot and")
        print("        cannot be repaired from this repository, so it does not fail")
        print("        this command. Remove the synced entry in the app's skill")
        print("        manager; the next sync re-imports the corrected copy.")
        return 0

    print("  PASS  the account copy matches too.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
