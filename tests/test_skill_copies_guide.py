"""Guard for SKILL-COPIES.md, the guide to where the skill lives.

Four files with the same name can live on one machine at once — the canonical
skill in this checkout, the fan-out `sync_skill.py` installs into every harness
(links to the canonical directory, or copies with `--copy`), the Freebuff Desktop
account copy under `~/.config/freebuff-desktop/account-skills/`, and the published
copy under `Veyra/skills/`. All four are built from the scripts below, so the guide
is not prose: every path, marker, flag, target and exit code in it is a claim about
code that can change underneath it.

A guide that names a path the script no longer uses is the same defect as a status
that contradicts the log, so it gets a test. Each assertion below fails if the code
moves and the guide does not.

Nothing here reads or writes the real home or the real account store: it reads the
repository and the constants the scripts declare.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "SKILL-COPIES.md"
README = REPO_ROOT / "README.md"
MANIFEST = REPO_ROOT / "MANIFEST.in"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_skill.py"
ACCOUNT_SCRIPT = REPO_ROOT / "scripts" / "check_account_skill.py"

PUBLISHED_SKILL = "Veyra/skills/veyra-proof"


def _load(path: Path, name: str):
    """Import a script that is not a package module, the way the other guards do."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sync():
    return _load(SYNC_SCRIPT, "skill_copies_guide_sync")


def _account():
    return _load(ACCOUNT_SCRIPT, "skill_copies_guide_account")


def _guide() -> str:
    assert GUIDE.is_file(), f"missing contributor guide: {GUIDE.name}"
    return GUIDE.read_text(encoding="utf-8")


def _flat() -> str:
    """The guide with markdown wrapping collapsed, so a reflow cannot fail a test."""
    return " ".join(_guide().split())


class TestTheGuideDescribesTheCodeThatExists:
    """Every path in the guide is a path some script resolves."""

    def test_the_canonical_path_is_the_one_the_fan_out_uses(self):
        canonical = _sync().CANONICAL_REL.as_posix()
        assert canonical in _guide(), (
            f"the guide must name the canonical skill as {canonical}, "
            "which is the path sync_skill.py compares everything against"
        )

    def test_the_ownership_marker_is_named_the_way_the_script_names_it(self):
        marker = _sync().MARKER
        assert marker in _guide(), (
            f"the guide must name {marker}: it is the file that decides whether a "
            "directory may be pruned"
        )

    def test_the_account_store_path_is_the_one_the_checker_inspects(self):
        store = "/".join(_account().DEFAULT_STORE_REL.parts)
        assert store in _guide(), (
            f"the guide must name the account store as {store}, "
            "which is what check_account_skill.py reads"
        )

    def test_every_global_fan_out_target_is_listed(self):
        """Adding a harness to GLOBAL_TARGETS must fail until the guide is updated.

        The target has to appear **unbroken**. A path split across two lines reads
        as a space to every markdown renderer, so `~/.goose/\nskills` is a target
        the reader cannot copy — and the same split is what a careless reflow
        produces, which is why this checks the wrapped text rather than the raw
        lines and still expects the whole path.
        """
        guide = _flat()
        missing = [
            target.skills_dir.format(home="~")
            for target in _sync().GLOBAL_TARGETS
            if target.skills_dir.format(home="~") not in guide
        ]
        assert not missing, (
            f"guide does not list these targets unbroken: {', '.join(missing)} — "
            "a target missing here was either removed from the code or wrapped "
            "across two lines"
        )

    def test_the_published_copy_is_described_where_it_lives(self):
        assert PUBLISHED_SKILL in _guide(), (
            "the guide must name the published copy, the one the sdist carries"
        )

    def test_every_script_it_tells_you_to_run_exists(self):
        named = set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", _guide()))
        assert named, "the guide names no script, so it cannot be checked"
        for name in sorted(named):
            assert (REPO_ROOT / "scripts" / name).is_file(), (
                f"the guide tells the reader to run scripts/{name}, which does not exist"
            )

    def test_it_names_the_commands_a_contributor_has_to_know(self):
        guide = _guide()
        for name in ("sync_skill.py", "check_skills.py", "check_account_skill.py", "pre_release.py"):
            assert name in guide, f"the guide never mentions {name}"

    def test_every_flag_it_tells_you_to_pass_exists(self):
        """A guide is a set of instructions; an invented flag is an instruction that fails.

        Asked of the script's own ``--help`` rather than of a copy of the parser, so
        a flag removed from the code fails this test instead of leaving the guide
        telling a contributor to run something that does not exist.
        """
        named = set(re.findall(r"sync_skill\.py ((?:--[a-z-]+ ?)+)", _flat()))
        flags = {flag for group in named for flag in group.split()}
        assert flags, "the guide names no sync_skill.py flag, so there is nothing to check"

        completed = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        missing = sorted(flag for flag in flags if flag not in completed.stdout)
        assert not missing, f"the guide names flags the script does not accept: {missing}"


class TestTheGuideKeepsItsShape:
    def test_one_table_row_per_copy_of_the_skill(self):
        rows = [line for line in _guide().splitlines() if line.startswith("| **")]
        assert len(rows) == 4, f"expected four copies, found {len(rows)}"
        for row in rows:
            cells = [cell for cell in row.split("|")[1:-1]]
            assert len(cells) == 7, f"row is missing ownership columns: {row}"

    def test_every_copy_row_names_an_owner_and_a_repair(self):
        """The question the guide exists to answer: whose is this, and who fixes it."""
        header = next(line for line in _guide().splitlines() if line.startswith("| Copy "))
        for column in ("Who writes it", "Whose it is", "Watched by", "Put right by"):
            assert column in header, f"the copy table lost its {column} column"


class TestTheGuideStaysHonest:
    def test_the_account_copy_is_reported_as_read_only_and_app_owned(self):
        flat = _flat()
        assert "the Freebuff Desktop app" in flat, (
            "the guide must name the app as the writer of the account copy"
        )
        assert "never synced" in flat, (
            "the guide must say the account store is checked, not written"
        )
        assert "overwritten in about a second" in flat, (
            "the guide must say why a local edit there does not survive"
        )

    def test_the_policy_is_warn_not_fail_and_says_why(self):
        flat = _flat()
        assert "warns and passes" in flat, (
            "the guide must state the account copy does not fail the check"
        )
        assert "cannot repair" in flat, (
            "the guide must give the reason: this repository cannot repair that cache"
        )
        assert "cannot run at all still fails" in flat, (
            "the guide must keep the exception: a checker that cannot run is a defect"
        )

    def test_the_only_editable_copy_is_named_as_such(self):
        flat = _flat()
        assert "the copy you edit" in flat, (
            "the guide must say plainly which copy a contributor may edit"
        )
        assert "not ours" in flat, "the guide must say which copy is not this repo's"

    def test_the_limit_of_the_release_gate_is_stated_rather_than_implied(self):
        assert "pre-tag hook" in _flat(), (
            "the guide may not imply git enforces this: it must say git has no "
            "pre-tag hook and that a direct `git tag` bypasses the gate"
        )

    def test_it_does_not_promise_to_repair_what_it_cannot(self):
        flat = _flat()
        assert "the app's skill manager" in flat, (
            "the guide must name the one lever that does exist for the Cloud copy"
        )
        assert "not a source of truth" in flat, (
            "the guide must say the account copy is not authoritative"
        )


class TestTheGuideStatesWhatTheLinkCosts:
    """The half of the change a reader has to be told, not left to discover.

    Linking the fan-out removes drift and buys a dependency on this checkout in
    exchange. A guide that sold the first half silently would be describing a
    different script, so each cost is pinned to a sentence that has to survive.
    """

    def test_it_says_the_fan_out_is_linked_not_copied(self):
        flat = _flat()
        assert "symlink" in flat, "the guide must say what a target is now"
        assert _sync().MODE_LINK in flat and _sync().MODE_COPY in flat, (
            "the guide must name both modes the script implements"
        )

    def test_it_says_a_link_is_only_as_durable_as_the_checkout(self):
        assert "only as durable as this checkout" in _flat(), (
            "the guide must say that moving or deleting the checkout kills the targets"
        )

    def test_it_says_what_happens_without_symlink_support(self):
        flat = _flat()
        assert "Developer Mode" in flat, (
            "the guide must name the platform where the fallback applies"
        )
        assert "falls back to a stamped copy" in flat, (
            "the guide must say the fallback installs a copy, not that it fails"
        )

    def test_it_is_explicit_about_what_could_not_be_verified(self):
        """A claim outside this repo has to be marked as one, not asserted."""
        flat = _flat()
        assert "statSync" in flat, (
            "the guide must cite the app behaviour it relies on rather than assuming it"
        )
        assert "could not be checked from here" in flat, (
            "the guide must mark the part that was not verified"
        )

    def test_it_says_the_published_installer_still_copies(self):
        flat = _flat()
        assert "Veyra/install.py" in flat and "still copies" in flat, (
            "the two installers differ on purpose; the guide must not imply otherwise"
        )


class TestTheGuideReachesItsReaders:
    def test_the_readme_points_at_it(self):
        readme = README.read_text(encoding="utf-8")
        assert GUIDE.name in readme, (
            "a contributor reading the README never finds the guide"
        )

    def test_the_sdist_carries_it(self):
        """The archive ships the guide, and the two skill copies the guide claims.

        The guide tells a reader of the sdist which places exist and which of them
they are holding. That claim is only true while `MANIFEST.in` lists both — the
        canonical skill and the published copy — so both are asserted against the
        manifest rather than assumed, and the absence of a harness fan-out is stated
        in the guide as the reason there is nothing to link in that archive.
        """
        manifest = MANIFEST.read_text(encoding="utf-8")
        assert GUIDE.name in manifest, (
            "the guide must ship in the sdist, which carries two copies of the skill"
        )
        for copy in (_sync().CANONICAL_REL.as_posix(), PUBLISHED_SKILL):
            assert copy in manifest, (
                f"the guide says the sdist carries {copy}, and MANIFEST.in disagrees"
            )
        assert "no harness fan-out" in _flat(), (
            "the guide must say why the archive carries no fan-out"
        )
