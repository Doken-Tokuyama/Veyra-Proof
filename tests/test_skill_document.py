"""Guards for the canonical Veyra skill document and its multi-harness fan-out.

The skill is mutated by more than a human: EvoForge's ``mcp_mutate_skill``, the
GEPA guard skills, and any future skill optimizer all edit this file. These
tests fail the build when a mutation erodes the safety envelope or the
harness-neutral portability contract.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / ".cursor" / "skills" / "veyra-proof"
SKILL_MD = SKILL_DIR / "SKILL.md"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_skill.py"
ACCOUNT_SCRIPT = REPO_ROOT / "scripts" / "check_account_skill.py"
COPIES_GUIDE = REPO_ROOT / "SKILL-COPIES.md"
PUBLISH_DIR = REPO_ROOT / "Veyra"
PUBLISH_SKILL_DIR = PUBLISH_DIR / "skills" / "veyra-proof"
PUBLISH_INSTALLER = PUBLISH_DIR / "install.py"

HARD_RULES_START = "<!-- HARD_RULES_START -->"
HARD_RULES_END = "<!-- HARD_RULES_END -->"

# Must match sync_skill.CANONICAL_REL; asserted below so the two cannot drift.
CANONICAL_REL = Path(".cursor") / "skills" / "veyra-proof"

# Must match sync_skill.MARKER, for the same reason.
MARKER = ".veyra-skill-sync"

# Paths that only ever resolve on one machine. A skill carrying these stops
# working the moment it is copied to another checkout, another OS, or another
# agent harness.
MACHINE_LOCAL_PATTERNS = (
    re.compile(r"/Users/"),
    re.compile(r"/Volumes/"),
    re.compile(r"/home/[a-z]"),
    re.compile(r"[A-Z]:\\\\"),
)

# Prohibitions that must survive every mutation, spelled as fragments so a
# harmless rewording does not fail the build but a deletion does.
REQUIRED_HARD_RULES = (
    "Never invent tool output",
    "curl | sh",
    "Never mutate `Cargo.toml`",
    "cargo update",
    "--install-missing",
    "MISSING",
)

# SKILL.md is loaded into the agent's context on every trigger, so maintainer
# material must live in the README. SkillOpt's own guidance is 300-2,000 tokens;
# the lower ceiling here is deliberate headroom, not a contradiction.
MAX_SKILL_TOKENS = 1600


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _hard_rules_block() -> str:
    text = _skill_text()
    start = text.index(HARD_RULES_START)
    end = text.index(HARD_RULES_END)
    return text[start:end]


def test_skill_document_exists() -> None:
    assert SKILL_MD.is_file()
    assert (SKILL_DIR / "reference.md").is_file()


def test_canonical_path_matches_the_sync_script(sync) -> None:
    assert sync.CANONICAL_REL == CANONICAL_REL
    assert SKILL_DIR == REPO_ROOT / CANONICAL_REL


def test_marker_name_matches_the_sync_script(sync) -> None:
    """A renamed marker would orphan every copy the old name had stamped."""
    assert sync.MARKER == MARKER


def test_frontmatter_declares_name_and_description() -> None:
    text = _skill_text()
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    frontmatter = text.split("---", 2)[1]
    assert re.search(r"^name:\s*veyra-proof\s*$", frontmatter, re.M)
    assert re.search(r"^description:", frontmatter, re.M)


def test_hard_rules_markers_are_balanced() -> None:
    text = _skill_text()
    assert text.count(HARD_RULES_START) == 1, "expected exactly one frozen region"
    assert text.count(HARD_RULES_END) == 1
    assert text.index(HARD_RULES_START) < text.index(HARD_RULES_END)


def test_hard_rules_survive_mutation() -> None:
    block = _hard_rules_block()
    missing = [rule for rule in REQUIRED_HARD_RULES if rule not in block]
    assert not missing, f"hard rules lost: {missing}"


def test_frozen_region_is_announced_as_frozen() -> None:
    """A marker nothing honours is decoration; the freeze has to be stated."""
    assert "frozen" in _hard_rules_block().lower()


def test_skill_carries_no_machine_local_paths() -> None:
    for name in ("SKILL.md", "reference.md"):
        text = (SKILL_DIR / name).read_text(encoding="utf-8")
        for pattern in MACHINE_LOCAL_PATTERNS:
            match = pattern.search(text)
            assert match is None, (
                f"{name} contains a machine-local path {match.group(0)!r}; "
                "use an env var or a relative path so the skill stays portable"
            )


def test_skill_stays_within_token_budget() -> None:
    approx_tokens = len(_skill_text()) / 4
    assert approx_tokens < MAX_SKILL_TOKENS, (
        f"skill is ~{approx_tokens:.0f} tokens, over the {MAX_SKILL_TOKENS} budget"
    )


def test_readme_documents_every_declared_harness(sync) -> None:
    """Each harness the sync script targets must be named in the README table."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    labels = {t.label for t in sync.GLOBAL_TARGETS} | {
        t.label for t in sync.PROJECT_TARGETS
    }
    undocumented = sorted(label for label in labels if label.lower() not in readme.lower())
    assert not undocumented, f"harnesses missing from the README table: {undocumented}"


def test_skill_holds_no_maintainer_facing_sections() -> None:
    """Maintainer material in SKILL.md is dead weight in the agent's context."""
    headings = set(re.findall(r"^## (.+)$", _skill_text(), re.M))
    for banned in ("Harness portability", "Mutation protocol", "Maintaining"):
        assert banned not in headings, (
            f"'{banned}' belongs in the README, not in SKILL.md"
        )


# --------------------------------------------------------------------------
# sync_skill.py
# --------------------------------------------------------------------------


def _load_account():
    """Import scripts/check_account_skill.py, to read the store it inspects."""
    spec = importlib.util.spec_from_file_location("check_account_skill", ACCOUNT_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_sync():
    """Import scripts/sync_skill.py, which is a script rather than a package module."""
    spec = importlib.util.spec_from_file_location("sync_skill", SYNC_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolves annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _links_supported() -> bool:
    """Ask the code under test, once, whether this filesystem can hold a symlink.

    Windows without Developer Mode cannot, and neither can a home on FAT or some
    SMB mounts. The link behaviour is then unreachable by design — the script
    falls back to a copy and says so — so those tests are skipped here rather than
    left to fail on a platform where the feature is not available to test. The
    fallback itself is driven directly by
    :func:`test_a_machine_without_symlinks_falls_back_to_a_copy_and_says_so`,
    which runs everywhere.
    """
    spec = importlib.util.spec_from_file_location("sync_skill_probe", SYNC_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.links_supported()


needs_links = pytest.mark.skipif(
    not _links_supported(),
    reason="this filesystem cannot create symlinks, so link mode installs copies",
)


@pytest.fixture()
def sync(tmp_path, monkeypatch):
    """The sync module, with its audit log redirected into the tmp tree."""
    module = _load_sync()
    monkeypatch.setattr(module, "LOG_PATH", tmp_path / "sync.log")
    return module


def _seed_skill(root: Path) -> Path:
    canonical = root / CANONICAL_REL
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text(
        "---\nname: veyra-proof\n---\n# Skill\n", encoding="utf-8"
    )
    (canonical / "reference.md").write_text("# Reference\n", encoding="utf-8")
    return canonical


def _sandbox(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """A repo root and a home directory that never overlap, plus common argv.

    Keeping them distinct matters: when root == home the canonical copy and the
    cursor global target collide, which is exactly the de-duplication this
    module is supposed to perform and exactly what these tests must not assert
    against.
    """
    root = tmp_path / "repo"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    return root, home, ["--root", str(root), "--home", str(home)]


def test_sync_targets_have_unique_paths(sync, tmp_path) -> None:
    root, home, _ = _sandbox(tmp_path)
    targets = sync.select_targets(
        root=root, home=home, include_project=True, include_absent=True
    )
    resolved = [t.resolve(root, home).resolve() for t in targets]
    assert len(resolved) == len(set(resolved)), "two targets resolve to the same path"


def test_sync_never_targets_the_canonical_copy(sync, tmp_path) -> None:
    root, home, _ = _sandbox(tmp_path)
    _seed_skill(root)
    targets = sync.select_targets(
        root=root, home=home, include_project=True, include_absent=True
    )
    canonical = (root / CANONICAL_REL).resolve()
    resolved = {t.resolve(root, home).resolve() for t in targets}
    assert canonical not in resolved


def test_sync_skips_absent_harnesses_unless_forced(sync, tmp_path) -> None:
    root, home, _ = _sandbox(tmp_path)
    (home / ".cursor").mkdir()
    (home / ".claude").mkdir()
    detected = sync.select_targets(
        root=root, home=home, include_project=False, include_absent=False
    )
    assert {t.label for t in detected} == {"cursor", "claude"}

    forced = sync.select_targets(
        root=root, home=home, include_project=False, include_absent=True
    )
    assert len(forced) > len(detected)


def test_check_mode_reports_drift_then_succeeds_after_sync(sync, tmp_path) -> None:
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()

    assert sync.main(argv + ["--check"]) == 1, "check must fail while the copy is missing"
    assert sync.main(argv) == 0
    assert sync.main(argv + ["--check"]) == 0, "check must pass once the copy matches"


def test_check_reports_vacuous_success_honestly(sync, tmp_path, capsys) -> None:
    """With no harness installed, --check must not claim 'all targets in sync'."""
    root, _, argv = _sandbox(tmp_path)
    _seed_skill(root)

    assert sync.main(argv + ["--check"]) == 0
    out = capsys.readouterr().out
    assert "all targets in sync" not in out
    assert "no installed harness detected" in out


def test_check_says_which_copy_it_reports_and_who_owns_it(sync, tmp_path, capsys) -> None:
    """Four copies of this skill can be on a machine; the report has to place itself.

    A reader who finds a copy and runs the check should not need SKILL-COPIES.md to
    learn whether this is the copy they may edit.
    """
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()

    sync.main(argv + ["--check"])
    out = capsys.readouterr().out
    assert sync.REPORTED_COPY in out
    assert "harness fan-out" in out
    assert "owned by this repository" in out
    assert "the copy you edit" in out


def test_check_names_the_copies_it_does_not_report(sync, tmp_path, capsys) -> None:
    """Staying silent about the other copies reads as 'and those are fine too'."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()

    sync.main(argv + ["--check"])
    out = capsys.readouterr().out
    # The report renders paths POSIX-style on purpose, so this is what a reader on
    # any platform sees; comparing `str(Path)` would only hold on POSIX.
    assert sync.ACCOUNT_STORE_REL.as_posix() in out
    assert "Freebuff Desktop app" in out
    assert "check_account_skill.py" in out
    assert sync.PUBLISHED_REL.as_posix() in out
    assert "byte-identical" in out


def test_the_two_scripts_name_the_same_account_store(sync) -> None:
    """The report's pointer to the account check must point at the real store."""
    assert _load_account().DEFAULT_STORE_REL == sync.ACCOUNT_STORE_REL


def test_the_legend_prints_even_with_no_harness_detected(sync, tmp_path, capsys) -> None:
    """The run with nothing to report is the one where a reader needs the map."""
    root, _, argv = _sandbox(tmp_path)
    _seed_skill(root)

    assert sync.main(argv + ["--check"]) == 0
    out = capsys.readouterr().out
    assert "no installed harness detected" in out
    assert "harness fan-out" in out
    assert "Cloud account copy" in out


def test_the_write_path_names_the_same_copies(sync, tmp_path, capsys) -> None:
    """One report, one frame: the legend is not a --check-only decoration."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()

    assert sync.main(argv) == 0
    out = capsys.readouterr().out
    assert sync.REPORTED_COPY in out
    assert "not owned or reported here" in out


def test_the_report_and_the_guide_name_the_same_owners(sync) -> None:
    """The output and SKILL-COPIES.md may not tell the reader different stories."""
    guide = COPIES_GUIDE.read_text(encoding="utf-8")
    assert sync.ACCOUNT_STORE_REL.as_posix() in guide
    assert sync.PUBLISHED_REL.as_posix() in guide
    assert "Freebuff Desktop app" in guide


@needs_links
def test_an_edit_is_live_in_every_harness_without_a_sync(sync, tmp_path) -> None:
    """The property the links buy, and the reason the old drift test is gone.

    This used to write an edit into canonical, watch `--check` fail, and then run
    the sync to repair three copies. That sequence cannot happen now: a target IS
    the canonical directory, so the edit is live everywhere the moment it is
    saved, and there is no window in which a harness reads the older text. The
    assertion below is therefore stronger than the one it replaces — the copy
    mode still drifts, and `test_copy_mode_still_detects_drift` drives that path.
    """
    root, home, argv = _sandbox(tmp_path)
    canonical = _seed_skill(root)
    for home_dir in (".cursor", ".claude", ".codex"):
        (home / home_dir).mkdir()

    assert sync.main(argv) == 0

    v2 = "---\nname: veyra-proof\n---\n# v2\n"
    (canonical / "SKILL.md").write_text(v2, encoding="utf-8")

    assert sync.main(argv + ["--check"]) == 0, "a live edit is not drift"

    for home_dir in (".cursor", ".claude", ".codex"):
        target = home / home_dir / "skills" / "veyra-proof"
        assert target.is_symlink(), f"{target} is a copy, not a link"
        assert (target / "SKILL.md").read_text(encoding="utf-8") == v2
        assert (target / "reference.md").is_file()


def test_copy_mode_still_detects_drift_and_repairs_it(sync, tmp_path) -> None:
    """The fallback keeps the old contract, because a copy can still go stale."""
    root, home, argv = _sandbox(tmp_path)
    canonical = _seed_skill(root)
    (home / ".cursor").mkdir()

    assert sync.main(argv + ["--copy"]) == 0
    copied = home / ".cursor" / "skills" / "veyra-proof"
    assert not copied.is_symlink(), "--copy must install a real copy"

    (canonical / "SKILL.md").write_text("# v2\n", encoding="utf-8")
    assert sync.main(argv + ["--copy", "--check"]) == 1
    assert sync.main(argv + ["--copy"]) == 0
    assert sync.main(argv + ["--copy", "--check"]) == 0
    assert (copied / "SKILL.md").read_text(encoding="utf-8") == "# v2\n"


@needs_links
def test_a_copy_installed_with_copy_mode_is_accepted_by_the_default_check(
    sync, tmp_path, capsys
) -> None:
    """A machine that chose copies must not be reported as broken forever.

    The marker is what makes that safe to allow: it says the directory was
    installed on purpose, so `--check` reports it as its own kind of correct
    rather than as a duplicate to repair. An *unstamped* matching copy is still
    repaired — see the conversion test below — because that one arrived by hand.
    """
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()

    assert sync.main(argv + ["--copy"]) == 0
    capsys.readouterr()

    assert sync.main(argv + ["--check"]) == 0
    out = capsys.readouterr().out
    assert "installed on purpose with --copy" in out


@needs_links
def test_a_matching_copy_without_a_marker_is_converted_to_a_link(
    sync, tmp_path
) -> None:
    """A copy nobody declared is a duplicate, whatever it was meant to be."""
    root, home, argv = _sandbox(tmp_path)
    canonical = _seed_skill(root)
    (home / ".cursor").mkdir()

    target = home / ".cursor" / "skills" / "veyra-proof"
    target.mkdir(parents=True)
    for rel in sorted(path.name for path in canonical.iterdir()):
        (target / rel).write_bytes((canonical / rel).read_bytes())

    assert sync.main(argv + ["--check"]) == 1, "an undeclared duplicate is drift"
    assert sync.main(argv) == 0
    assert target.is_symlink()


def test_an_unmanaged_directory_holding_foreign_files_is_left_alone(
    sync, tmp_path, capsys
) -> None:
    """The guard that keeps the link mode from deleting somebody's work.

    A directory at the target holding a file this skill does not ship is not a
    copy of this skill, whatever it is called. It is reported, it fails the check
    so a human sees it, and no write run replaces it.
    """
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    target = home / ".cursor" / "skills" / "veyra-proof"
    target.mkdir(parents=True)
    (target / "notes.md").write_text("my own notes\n", encoding="utf-8")

    assert sync.main(argv + ["--check"]) == 1, "the check must not pass over this"
    assert sync.main(argv) == 0

    assert (target / "notes.md").read_text(encoding="utf-8") == "my own notes\n", (
        "a write run destroyed a file no script wrote"
    )
    assert "this skill does not have" in capsys.readouterr().out


def test_a_plain_file_at_a_target_is_left_alone(sync, tmp_path) -> None:
    """Not a directory, not a link: the script has no business guessing."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    target = home / ".cursor" / "skills" / "veyra-proof"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a file where a directory goes\n", encoding="utf-8")

    assert sync.main(argv + ["--check"]) == 1
    assert sync.main(argv) == 0
    assert target.read_text(encoding="utf-8") == "a file where a directory goes\n"


@needs_links
def test_a_link_pointing_somewhere_else_is_relinked(sync, tmp_path, capsys) -> None:
    """Somebody's working copy is not this checkout, and the note says which is which."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    elsewhere = tmp_path / "someone-elses-skill"
    elsewhere.mkdir()
    (elsewhere / "SKILL.md").write_text("# their edit\n", encoding="utf-8")
    target = home / ".cursor" / "skills" / "veyra-proof"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(elsewhere)

    assert sync.main(argv + ["--check"]) == 1
    assert sync.main(argv) == 0
    assert target.is_symlink()
    assert target.resolve() == (root / CANONICAL_REL).resolve()
    assert (elsewhere / "SKILL.md").read_text(encoding="utf-8") == "# their edit\n", (
        "relinking destroyed the directory the old link pointed at"
    )
    assert "points somewhere else" in capsys.readouterr().out


@needs_links
def test_a_link_into_a_moved_checkout_is_repaired_and_says_why(
    sync, tmp_path, capsys
) -> None:
    """The honest cost of a link: it is only as durable as the checkout it names.

    Moving or deleting the checkout leaves every harness target dangling. That is
    a real regression against the copy mode, so it is pinned here with the repair
    rather than mentioned in prose and hoped for.
    """
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0
    target = home / ".cursor" / "skills" / "veyra-proof"
    assert target.is_dir()
    capsys.readouterr()

    target.unlink()
    target.symlink_to(tmp_path / "the-old-place" / "repo" / CANONICAL_REL)

    assert sync.main(argv + ["--check"]) == 1
    out = capsys.readouterr().out
    assert "moved or was deleted" in out, "a dead link must not read as an ordinary difference"
    assert sync.main(argv) == 0
    assert (target / "SKILL.md").is_file(), "the repair did not produce a readable skill"


def test_a_machine_without_symlinks_falls_back_to_a_copy_and_says_so(
    sync, tmp_path, monkeypatch
) -> None:
    """Windows without Developer Mode is a real machine, not an error case.

    A check that cannot pass on a platform is one people learn to bypass, so the
    fallback installs a stamped copy and `--check` accepts it — and the report
    says why, so nobody concludes the links are in place.
    """
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    monkeypatch.setattr(sync, "links_supported", lambda: False)

    assert sync.main(argv) == 0
    target = home / ".cursor" / "skills" / "veyra-proof"
    assert not target.is_symlink(), "a copy was expected where links are unavailable"
    assert (target / sync.MARKER).is_file()
    assert sync.main(argv + ["--check"]) == 0
    assert (target / "SKILL.md").is_file()


def test_the_report_names_the_mode_it_is_working_in(sync, tmp_path, capsys) -> None:
    """Link or copy is the first thing a reader needs to know about a run."""
    root, _, argv = _sandbox(tmp_path)
    _seed_skill(root)

    sync.main(argv + ["--check"])
    assert "mode       link" in capsys.readouterr().out

    sync.main(argv + ["--copy", "--check"])
    assert "mode       copy" in capsys.readouterr().out


def test_sync_never_touches_the_real_home(sync, tmp_path) -> None:
    """Regression guard: a sandboxed run must not reach the user's real home."""
    root, _, argv = _sandbox(tmp_path)
    _seed_skill(root)
    assert sync.main(argv) == 0
    # Windows keeps %TEMP% inside the user profile, so "outside the real home"
    # is unachievable for any temp file there and a prefix comparison against
    # Path.home() fails on every Windows run. The invariant that holds on all
    # platforms is containment in the sandbox: a run that reset LOG_PATH to
    # Path.home() lands outside it on Windows as well.
    assert sync.LOG_PATH.resolve().is_relative_to(tmp_path.resolve())


def test_write_atomic_leaves_no_temp_file(sync, tmp_path) -> None:
    destination = tmp_path / "nested" / "SKILL.md"
    sync.write_atomic(destination, b"payload")
    assert destination.read_bytes() == b"payload"
    assert list(destination.parent.iterdir()) == [destination]


# --------------------------------------------------------------------------
# --prune: copies left behind by a rename
# --------------------------------------------------------------------------


def _stamped_copy(parent: Path, name: str, skill_name: str) -> Path:
    """A directory shaped like a copy this sync script is known to own."""
    directory = parent / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text("stale\n", encoding="utf-8")
    (directory / MARKER).write_text(f"{skill_name}\n", encoding="utf-8")
    return directory


@needs_links
def test_a_synced_target_is_a_link_and_stamps_nothing(sync, tmp_path) -> None:
    """A link needs no ownership stamp, and must not write one where it would land.

    ``<target>/.veyra-skill-sync`` inside a linked target resolves to the *repo's*
    own skill directory. The old copy mode stamped what it wrote; the link mode has
    to prove ownership by its target instead, or the stamp lands in canonical.
    """
    root, home, argv = _sandbox(tmp_path)
    canonical = _seed_skill(root)

    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0

    target = home / ".cursor" / "skills" / CANONICAL_REL.name
    assert target.is_symlink()
    assert target.resolve() == canonical.resolve()
    assert not (canonical / sync.MARKER).exists(), (
        "the copy stamp was written into the canonical skill"
    )


def test_copy_mode_stamps_a_healthy_copy_that_lost_its_marker(sync, tmp_path) -> None:
    """Unstamped copies are unprunable, so copy mode still backfills the stamp."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv + ["--copy"]) == 0

    copied = home / ".cursor" / "skills" / CANONICAL_REL.name
    (copied / sync.MARKER).unlink()

    assert sync.main(argv + ["--copy"]) == 0
    assert (copied / sync.MARKER).is_file()
    # Two lines: who wrote it, and the mode it was installed in. The mode word is
    # what a later run reads to tell a declared copy from a duplicate.
    stamp = (copied / sync.MARKER).read_text(encoding="utf-8").splitlines()
    assert stamp[0] == CANONICAL_REL.name
    assert stamp[1] == sync.MODE_COPY
    assert sync.marker_mode(copied) == sync.MODE_COPY


@needs_links
def test_a_copy_stamped_by_the_older_script_is_converted_not_accepted(
    sync, tmp_path
) -> None:
    """The migration this change needs, and the reason the stamp got a second line.

    Every machine already carrying a stamped copy has a directory that proves it is
    ours but says nothing about a human choosing copies. Reading that stamp as a
    declaration would leave the fan-out duplicated behind a green check — the exact
    situation the link mode exists to end — so it converges like any other
    duplicate, while `--prune` keeps trusting it as ours.
    """
    root, home, argv = _sandbox(tmp_path)
    canonical = _seed_skill(root)
    (home / ".cursor").mkdir()

    target = home / ".cursor" / "skills" / CANONICAL_REL.name
    target.mkdir(parents=True)
    for path in sorted(canonical.iterdir()):
        (target / path.name).write_bytes(path.read_bytes())
    (target / sync.MARKER).write_text(f"{CANONICAL_REL.name}\n", encoding="utf-8")
    assert sync.marker_mode(target) == "", "an older stamp declares no mode"
    assert sync.is_managed(target), "an older stamp still proves ownership"

    assert sync.main(argv + ["--check"]) == 1
    assert sync.main(argv) == 0
    assert target.is_symlink()


def test_check_writes_nothing_at_all(sync, tmp_path) -> None:
    """--check promises to change nothing; a hidden write would break that."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()

    assert sync.main(argv + ["--check"]) == 1
    target = home / ".cursor" / "skills" / CANONICAL_REL.name
    assert not target.exists() and not target.is_symlink(), "--check created the target"
    assert not (target / sync.MARKER).exists()


def test_rename_leaves_a_target_that_only_prune_can_see(sync, tmp_path, monkeypatch) -> None:
    """The regression this mode exists for.

    After a rename, --check is perfectly happy: the new name is installed and
    reads the canonical skill. Nothing in the old harness flow ever looks at the
    old name, which is how a retired skill keeps answering as if it were live.
    """
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0

    monkeypatch.setattr(sync, "SKILL_NAME", "veyra-next")
    assert sync.main(argv) == 0, "the new name gets installed"
    assert sync.main(argv + ["--check"]) == 0, "--check is now perfectly satisfied"

    retired = home / ".cursor" / "skills" / "veyra-proof"
    assert retired.exists(), (
        "the retired target survives a green --check, still declares the old "
        "name, and keeps answering queries as if it were live"
    )


@needs_links
def test_prune_finds_a_link_left_dangling_by_a_rename(sync, tmp_path) -> None:
    """The realistic rename moves the canonical directory too, which kills the link.

    A dead link is not ``is_dir()``, so the existence test has to accept a link as
    well as a directory or the retired skill is invisible to every scan — the same
    blind spot, one shape over.
    """
    root, home, argv = _sandbox(tmp_path)
    canonical = _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0

    renamed = canonical.parent / "veyra-next"
    canonical.rename(renamed)
    sync.SKILL_NAME = "veyra-next"
    sync.CANONICAL_REL = Path(".cursor") / "skills" / "veyra-next"

    retired = home / ".cursor" / "skills" / "veyra-proof"
    assert retired.is_symlink() and not retired.exists(), "expected a dead link"

    assert sync.main(argv + ["--prune"]) == 1
    assert sync.main(argv + ["--prune", "--apply"]) == 0
    assert not retired.exists() and not retired.is_symlink()
    assert (home / ".cursor" / "skills" / "veyra-next" / "SKILL.md").is_file()


def test_prune_is_a_dry_run_by_default(sync, tmp_path, capsys) -> None:
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0

    _stamped_copy(home / ".cursor" / "skills", "veyra-old", "veyra-proof")

    assert sync.main(argv + ["--prune"]) == 1
    assert (home / ".cursor" / "skills" / "veyra-old").is_dir(), "dry run deleted"
    out = capsys.readouterr().out
    assert "nothing was deleted" in out
    assert "--prune --apply" in out


def test_prune_apply_removes_only_our_own_copies(sync, tmp_path) -> None:
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0
    current = home / ".cursor" / "skills" / CANONICAL_REL.name

    skills = home / ".cursor" / "skills"
    _stamped_copy(skills, "veyra-old", "veyra-proof")

    assert sync.main(argv + ["--prune", "--apply"]) == 0
    assert not (skills / "veyra-old").exists()
    assert current.is_symlink(), "the live target must survive its own prune"
    assert (current / "SKILL.md").is_file()


def test_prune_leaves_foreign_skills_untouched(sync, tmp_path) -> None:
    """The harness skills directory is shared with every other skill the user has."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0

    skills = home / ".cursor" / "skills"
    someone_elses = skills / "refactor-assistant"
    someone_elses.mkdir()
    (someone_elses / "SKILL.md").write_text("theirs\n", encoding="utf-8")

    _stamped_copy(skills, "veyra-old", "veyra-proof")

    assert sync.main(argv + ["--prune", "--apply"]) == 0
    assert someone_elses.is_dir(), "pruned a skill this script never wrote"
    assert (someone_elses / "SKILL.md").read_text(encoding="utf-8") == "theirs\n"


def test_prune_never_removes_the_canonical_copy(sync, tmp_path) -> None:
    """The source of truth is not litter, however it is named."""
    root, home, argv = _sandbox(tmp_path)
    canonical = _seed_skill(root)

    assert sync.main(argv + ["--prune", "--apply", "--project"]) == 0
    assert canonical.is_dir()
    assert (canonical / "SKILL.md").is_file()


def test_prune_on_a_clean_install_finds_nothing(sync, tmp_path, capsys) -> None:
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0

    assert sync.main(argv + ["--prune"]) == 0
    assert "no managed copy left behind" in capsys.readouterr().out


def test_installing_a_link_is_recorded_in_the_audit_log(sync, tmp_path) -> None:
    """The log is the record of what this script did to a shared directory."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()

    assert sync.main(argv) == 0
    log = sync.LOG_PATH.read_text(encoding="utf-8")
    assert "cursor/global" in log
    assert "link" in log


def test_prune_is_recorded_in_the_audit_log(sync, tmp_path) -> None:
    """A deletion is the one action here that cannot be undone by re-running."""
    root, home, argv = _sandbox(tmp_path)
    _seed_skill(root)
    (home / ".cursor").mkdir()
    assert sync.main(argv) == 0
    retired = _stamped_copy(home / ".cursor" / "skills", "veyra-old", "veyra-proof")

    assert sync.main(argv + ["--prune", "--apply"]) == 0
    log = sync.LOG_PATH.read_text(encoding="utf-8")
    assert "prune" in log
    assert "veyra-old" in log


def test_apply_without_prune_is_refused(sync, tmp_path, capsys) -> None:
    """--apply alone is ambiguous; make the user name the operation."""
    root, _, argv = _sandbox(tmp_path)
    _seed_skill(root)

    assert sync.main(argv + ["--apply"]) == 2
    assert "--prune" in capsys.readouterr().err


def test_check_and_apply_together_are_refused(sync, tmp_path, capsys) -> None:
    """--check writes nothing, so honouring --apply would be a lie."""
    root, _, argv = _sandbox(tmp_path)
    _seed_skill(root)

    assert sync.main(argv + ["--check", "--prune", "--apply"]) == 2
    assert "--check" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The publishable copy in Veyra/
# --------------------------------------------------------------------------


def test_published_skill_is_byte_identical_to_canonical() -> None:
    """The repo someone clones must ship exactly what the project develops."""
    assert PUBLISH_SKILL_DIR.is_dir(), f"missing publishable skill: {PUBLISH_SKILL_DIR}"
    canonical_files = {p.name for p in SKILL_DIR.iterdir() if p.is_file()}
    published_files = {p.name for p in PUBLISH_SKILL_DIR.iterdir() if p.is_file()}
    assert canonical_files == published_files, (
        f"file set drifted: canonical {sorted(canonical_files)} vs "
        f"published {sorted(published_files)}"
    )
    for name in sorted(canonical_files):
        assert (PUBLISH_SKILL_DIR / name).read_bytes() == (SKILL_DIR / name).read_bytes(), (
            f"{name} differs between the canonical skill and the published copy; "
            "re-copy it before publishing"
        )


def test_published_repo_is_self_contained() -> None:
    for rel in ("README.md", "LICENSE", "install.py"):
        assert (PUBLISH_DIR / rel).is_file(), f"missing {rel} in the publishable repo"


def test_published_skill_documents_no_local_paths() -> None:
    """This folder gets cloned by strangers, so the usual guards apply here too."""
    for path in sorted(PUBLISH_SKILL_DIR.rglob("*.md")):
        for pattern in MACHINE_LOCAL_PATTERNS:
            match = pattern.search(path.read_text(encoding="utf-8"))
            assert match is None, f"{path.name} leaks a machine-local path {match.group(0)!r}"


def test_published_installer_agrees_with_the_readme() -> None:
    installer = PUBLISH_INSTALLER.read_text(encoding="utf-8")
    readme = (PUBLISH_DIR / "README.md").read_text(encoding="utf-8").lower()
    names = sorted(set(re.findall(r'Harness\(\s*"([a-z]+)"', installer)))
    assert names, "no harness table found in install.py"
    missing = [
        name
        for name in names
        if not re.search(rf"\|\s*`?{re.escape(name)}`?\s*\|", readme)
    ]
    assert not missing, f"harnesses missing from the Veyra README table: {missing}"


def test_both_installers_declare_the_same_harnesses(sync) -> None:
    """A contributor's fan-out must reach everything the public installer reaches.

    The two tools live in different repositories once Veyra/ is published, so the
    table is duplicated on purpose. This test is what stops that duplication
    from drifting into a silently stale Cline or Goose copy.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("veyra_install", PUBLISH_INSTALLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    public = {h.name for h in module.HARNESSES}
    internal = {t.label for t in sync.GLOBAL_TARGETS} | {
        t.label for t in sync.PROJECT_TARGETS
    }
    assert internal == public, (
        f"harness tables drifted: only in sync_skill.py {sorted(internal - public)}, "
        f"only in Veyra/install.py {sorted(public - internal)}"
    )


def test_published_installer_is_valid_python() -> None:
    import py_compile

    py_compile.compile(str(PUBLISH_INSTALLER), doraise=True)


def test_published_installer_detects_harnesses(sync, tmp_path, capsys) -> None:
    """Smoke-test the public installer against a fake home, writing into tmp only."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("veyra_install", PUBLISH_INSTALLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    root = tmp_path / "repo"
    root.mkdir()

    rows = module.detect(root, fake_home, None)
    assert [harness.name for harness, _ in rows] == ["claude"]
    assert rows[0][1] == fake_home / ".claude" / "skills" / "veyra-proof"
    assert module.main(["--list"]) in (0, 1)
