"""Guards for scripts/check_published.py, the gate step that asks the index.

The step exists because the question a release asks is not "does this checkout build a
good artifact?" but "is the artifact users install the one that was checked?", and until
this step nothing in the checklist opened the published file at all. Cutting v0.5.0 is
the worked example: the comparison was made by hand, once, in a terminal, and it could
have come out differently without the gate noticing.

These tests therefore run a **real HTTP server** on loopback rather than patching
`urlopen`. The transport is half of what this script is — a 404 is the "nothing published
yet" answer, a refused connection is the "cannot verify" answer, and an index that
serves an absolute URL for a file is a different code path from the JSON request — so
stubbing it out would leave the part that failed the release untested. What the fixtures
synthesize is the *index*, not the network.

The other half, the comparison, is driven on synthetic archives because an archive is
just a mapping of names to bytes here; the real thing is exercised end to end against
PyPI by the gate itself, which is where `44 of 44` and `104 of 104` in the v0.5.0 notes
came from.

Provenance is the half that needs a *commit*, not just bytes: the step proves the
published files are the released commit's by reading that commit's tree with `git
archive`, so every fixture here commits the tree its archives are derived from. A
fixture whose artifacts and commit disagreed would be testing a disagreement that
cannot exist; the tests that need one to disagree make it disagree on purpose.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_published.py"

VERSION = "9.9.9"
DIST_NAME = "veyra-proof"
WHEEL = f"veyra_proof-{VERSION}-py3-none-any.whl"
SDIST = f"veyra_proof-{VERSION}.tar.gz"


@pytest.fixture()
def check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_published", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _zip(files: dict[str, bytes], *, when: int = 0) -> bytes:
    """A wheel-shaped zip. `when` changes the bytes without changing the payload."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, when % 60))
            archive.writestr(info, data)
    return buffer.getvalue()


def _tar(files: dict[str, bytes], *, when: int = 0) -> bytes:
    """An sdist-shaped tar.gz. Same trick: the member timestamps only move the bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = when
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# What the released commit holds. The wheel is the package, the sdist is the tree, and
# both are derived from this map — so the fixture's artifacts and its git tree cannot
# disagree unless a test makes them.
TREE = {
    "README.md": b"# readme\n",
    "src/veyra_proof/__init__.py": b'__version__ = "9.9.9"\n',
    "src/veyra_proof/cli.py": b"# the tool\n",
}
PAYLOAD = {
    "veyra_proof/__init__.py": TREE["src/veyra_proof/__init__.py"],
    "veyra_proof/cli.py": TREE["src/veyra_proof/cli.py"],
}
SDIST_PAYLOAD = {
    f"veyra-proof-{VERSION}/README.md": TREE["README.md"],
    **{
        f"veyra-proof-{VERSION}/{path}": data
        for path, data in TREE.items()
        if path.startswith("src/")
    },
}


class Index:
    """A real index on loopback, serving one release and its files."""

    def __init__(self, release: dict | None = None, files: dict[str, bytes] | None = None) -> None:
        self.release = release
        self.files = files or {}
        self.requests: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — named by the base class
                outer.requests.append(self.path)
                if self.path in outer.files:
                    body = outer.files[self.path]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                elif outer.release is not None and self.path.startswith("/pypi/"):
                    body = json.dumps(outer.release).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                else:
                    self.send_error(404, "Not Found")
                    return
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                """Quiet: a test that prints an access log is a test hard to read."""

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def release_payload(self, entries: list[str], *, yanked: bool = False) -> dict:
        """The JSON the index answers with, for the file names it is given.

        The digests are computed from the bytes this index will really serve, the way
        PyPI computes them from the files it stores: a fixture that advertised anything
        else would be testing a digest nothing in the world would send.
        """
        urls = []
        for name in entries:
            body = self.files[f"/files/{name}"]
            urls.append(
                {
                    "filename": name,
                    "url": f"{self.host}/files/{name}",
                    "digests": {"sha256": hashlib.sha256(body).hexdigest()},
                    "yanked": yanked,
                }
            )
        return {"info": {"name": DIST_NAME, "version": VERSION}, "urls": urls}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    """A committed checkout's artifacts: the two files the gate would have built.

    Built twice per shape with different member timestamps on purpose: that is what two
    builds of one commit actually produce, and the whole reason the comparison is by
    payload rather than by file. The tree underneath is committed before `dist/` is
    written into it, because the provenance half asks which commit the published files
    came from and must have an answer.
    """
    root = tmp_path / "repo"
    (root / "dist").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{DIST_NAME}"\nversion = "{VERSION}"\n', encoding="utf-8"
    )
    for path, data in TREE.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    _git_checkout(root)
    (root / "dist" / WHEEL).write_bytes(_zip(PAYLOAD, when=1))
    (root / "dist" / SDIST).write_bytes(_tar(SDIST_PAYLOAD, when=1))
    return root


def _run(check: ModuleType, root: Path, host: str, *extra: str, report: Path | None = None) -> Any:
    argv = ["--root", str(root), "--index-url", host, *extra]
    if report is not None:
        argv += ["--report", str(report)]
    return check.main(argv), report


def _facts(report: Path) -> dict:
    return json.loads(report.read_text(encoding="utf-8"))


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", message],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _git_checkout(root: Path) -> None:
    """A committed checkout, so the diagnosis of *which* tree this is has an answer."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    _commit(root, "seed")


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _mismatched_index(*, version: str = VERSION) -> Index:
    """An index serving this version, with one member that differs."""
    served = {**PAYLOAD, "veyra_proof/cli.py": b"# not what this checkout built\n"}
    index = Index(files={f"/files/{WHEEL}": _zip(served)})
    index.release = index.release_payload([WHEEL])
    return index


def test_the_published_payload_is_compared_member_by_member(
    check: ModuleType, dist: Path, tmp_path: Path
) -> None:
    """The claim: same members, same content — while the archives differ, as they do.

    Two builds of one commit are not byte-identical (a zip stores each member's
    timestamp), so a check comparing whole files would fail a perfectly good release.
    """
    index = Index(
        files={
            f"/files/{WHEEL}": _zip(PAYLOAD, when=2),
            f"/files/{SDIST}": _tar(SDIST_PAYLOAD, when=2),
        }
    )
    index.release = index.release_payload([WHEEL, SDIST])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    assert code == 0
    facts = _facts(report)["facts"]
    assert facts["published"] is True
    assert facts["files_compared"] == 2
    by_name = {entry["filename"]: entry for entry in facts["files"]}
    assert by_name[WHEEL]["members"] == len(PAYLOAD)
    assert by_name[WHEEL]["identical_members"] == len(PAYLOAD)
    assert by_name[WHEEL]["archive_identical"] is False, "the archives really do differ"
    assert by_name[SDIST]["identical_members"] == len(SDIST_PAYLOAD)


def test_a_member_whose_content_differs_fails_and_names_it(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure this step exists for: the index serves something else."""
    served = dict(PAYLOAD)
    served["veyra_proof/cli.py"] = b"# not the tool this gate checked\n"
    index = Index(files={f"/files/{WHEEL}": _zip(served)})
    index.release = index.release_payload([WHEEL])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "veyra_proof/cli.py differs" in out
    facts = _facts(report)
    assert facts["ok"] is False, "the report is a record of the run, not only of a pass"
    entry = facts["facts"]["files"][0]
    assert entry["differing_members"] == ["veyra_proof/cli.py"]
    assert entry["identical_members"] == len(PAYLOAD) - 1


def test_a_member_only_the_index_has_fails(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file the gate never saw is worse than a changed one: nothing checked it."""
    served = {**PAYLOAD, "veyra_proof/extra.py": b"# nobody checked this\n"}
    index = Index(files={f"/files/{WHEEL}": _zip(served)})
    index.release = index.release_payload([WHEEL])
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "veyra_proof/extra.py is in the file the index serves" in out


def test_a_served_file_this_checkout_cannot_show_fails(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The name is the pairing rule: a differently built artifact is a different one."""
    other = f"veyra_proof-{VERSION}-cp314-cp314-macosx_11_0_arm64.whl"
    index = Index(files={f"/files/{other}": _zip(PAYLOAD)})
    index.release = index.release_payload([other])
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "there is no file of that name" in out
    assert other in out


def test_a_version_the_index_does_not_have_passes_and_says_so(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Publishing happens after the gate, so this is the normal answer at tag time.

    It passes without claiming anything: the facts say `published: false`, which is what
    keeps the release notes from carrying a row that reads like a verification.
    """
    index = Index()  # no release: every /pypi/ path answers 404
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 0
    assert "there is nothing to compare" in out
    facts = _facts(report)["facts"]
    assert facts["published"] is False
    assert facts["files"] == []
    assert facts["reason"] == "the index has no files for this version yet"


def test_a_yanked_file_is_recorded_as_yanked(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Yanked means pip will not install it, so the claim needs the word in it."""
    index = Index(files={f"/files/{WHEEL}": _zip(PAYLOAD, when=5)})
    index.release = index.release_payload([WHEEL], yanked=True)
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    assert code == 0
    assert "yanked" in capsys.readouterr().out
    assert _facts(report)["facts"]["files"][0]["yanked"] is True


def test_byte_identity_is_reported_when_it_holds(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stronger claim, stated only when it is true rather than implied by the row."""
    wheel_bytes = (dist / "dist" / WHEEL).read_bytes()
    index = Index(files={f"/files/{WHEEL}": wheel_bytes})
    index.release = index.release_payload([WHEEL])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    assert code == 0
    assert "byte for byte" in capsys.readouterr().out
    assert _facts(report)["facts"]["files"][0]["archive_identical"] is True


def test_an_index_that_cannot_be_asked_is_not_a_pass(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unverifiable release is left unproven, the policy `platforms` already applies."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The port is now closed, so nothing is listening on it.

    code, _ = _run(check, dist, f"http://127.0.0.1:{port}")
    captured = capsys.readouterr()

    assert code == 2
    assert "cannot read" in captured.err
    assert "unproven rather than assumed" in captured.err


def test_both_spellings_of_an_index_url_ask_the_same_question(
    check: ModuleType, dist: Path, tmp_path: Path
) -> None:
    """pip calls it `…/simple`; the JSON API hangs off the host. Accept both."""
    index = Index(files={f"/files/{WHEEL}": _zip(PAYLOAD, when=3)})
    index.release = index.release_payload([WHEEL])
    try:
        code, _ = _run(check, dist, f"{index.host}/simple")
    finally:
        index.close()

    assert code == 0
    assert index.requests == [f"/pypi/{DIST_NAME}/{VERSION}/json", f"/files/{WHEEL}"]


def test_a_digest_that_does_not_describe_the_file_is_caught(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The index's own digest is checked against the bytes it served.

    Otherwise a mirror serving a corrupt body with a stale digest would pass on the
    strength of a number this check had repeated instead of computed.
    """
    index = Index(files={f"/files/{WHEEL}": _zip(PAYLOAD)})
    index.release = index.release_payload([WHEEL])
    index.release["urls"][0]["digests"]["sha256"] = "0" * 64
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "does not describe the file it served" in out


def test_an_artifact_that_cannot_be_read_is_a_failure_not_a_skip(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file this check cannot open cannot be compared, so it cannot be cleared."""
    (dist / "dist" / WHEEL).write_bytes(b"not an archive at all\n")
    index = Index(files={f"/files/{WHEEL}": b"neither is this\n"})
    index.release = index.release_payload([WHEEL])
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "cannot be read as a wheel" in out


def test_a_missing_pyproject_is_misuse(
    check: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 means the check could not run, which is not a verdict about an index."""
    bare = tmp_path / "bare"
    bare.mkdir()

    code, _ = _run(check, bare, "http://127.0.0.1:1")

    assert code == 2
    assert "cannot read the project name and version" in capsys.readouterr().err


def test_a_tree_that_is_not_the_released_one_says_so(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The likeliest reason for a difference, and the one a rebuild cannot fix.

    v0.5.0 is the real case: the version was published, work continued, and the
    artifacts this checkout builds are simply not the published ones. Without this line
    the failure reads as "your build is broken", which invites somebody to go and
    "fix" a build that is fine.
    """
    subprocess.run(["git", "tag", f"v{VERSION}"], cwd=dist, check=True, capture_output=True)
    (dist / "later.txt").write_text("work after the release\n", encoding="utf-8")
    _commit(dist, "later")

    index = _mismatched_index()
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert f"v{VERSION} names" in out
    assert "are not the published ones" in out
    assert "git worktree add" in out, "the message names the repair"


def test_a_difference_on_the_released_tree_is_not_explained_away(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no tag to blame, both readings are named and neither is claimed."""
    # The fixture's checkout is committed, but no v9.9.9 tag exists anywhere.
    index = _mismatched_index()
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "Either the published artifact is not the one this gate checked" in out
    assert "names" not in out.split("=== verdict", 1)[1][:200], (
        "no tag exists, so there is no other tree to point at"
    )


def test_it_runs_as_a_script(dist: Path, tmp_path: Path) -> None:
    """The gate invokes it as a subprocess, so that path is the one to check."""
    index = Index(
        files={
            f"/files/{WHEEL}": _zip(PAYLOAD, when=4),
            f"/files/{SDIST}": _tar(SDIST_PAYLOAD, when=4),
        }
    )
    index.release = index.release_payload([WHEEL, SDIST])
    report = tmp_path / "published.json"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(dist),
                "--index-url",
                index.host,
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        index.close()

    assert completed.returncode == 0, completed.stderr
    assert "2 file(s)" in completed.stdout
    assert _facts(report)["facts"]["files_compared"] == 2


def test_the_commit_tree_is_the_blobs_not_an_archive_of_rewritten_line_endings(
    check: ModuleType, dist: Path
) -> None:
    """`git archive` is not the commit when line endings are rewritten on the way out.

    GitHub's Windows runners set `core.autocrlf=true`; archive output then carries CRLF
    while the wheel carries the committed LF, so every text member was reported as
    "not what the released commit holds" on three Windows jobs (run 41882003503) over
    an artifact that matched its commit byte for byte. The tree must come from the
    objects, where nothing converts anything.
    """
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=dist, check=True)

    tree = check.commit_tree(dist, "HEAD")
    blob = subprocess.run(
        ["git", "cat-file", "blob", "HEAD:src/veyra_proof/cli.py"],
        cwd=dist,
        check=True,
        capture_output=True,
    ).stdout

    assert tree is not None
    assert tree["src/veyra_proof/cli.py"] == check.sha256_bytes(blob)


def test_the_published_files_are_traced_to_the_released_commit(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Provenance: which commit the world is installing, and how it was named."""
    index = Index(
        files={
            f"/files/{WHEEL}": _zip(PAYLOAD, when=2),
            f"/files/{SDIST}": _tar(SDIST_PAYLOAD, when=2),
        }
    )
    index.release = index.release_payload([WHEEL, SDIST])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 0
    assert "provenance: the published files are built from" in out
    facts = _facts(report)["facts"]
    assert facts["released_commit"] == _head(dist)
    assert facts["released_from"] == "HEAD"
    # The release notes cite the short form, so it has to be a recorded fact here:
    # this is the link between the step that measures and the row that states it.
    assert facts["released_commit_short"] == facts["released_commit"][:12]
    by_name = {entry["filename"]: entry for entry in facts["files"]}
    assert by_name[WHEEL]["provenance"]["members_matched"] == len(PAYLOAD)
    assert by_name[WHEEL]["provenance"]["proven"] is True
    assert by_name[SDIST]["provenance"]["members_matched"] == len(SDIST_PAYLOAD)


def test_a_file_built_from_another_commit_fails_even_when_the_gate_checked_it(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure provenance exists for, isolated from payload equality.

    `dist/` and the index agree with each other here — every member matches — and both
    disagree with the released commit. A check that only compares payloads calls this
    release fine; it is not fine: the world would be installing another commit's code.
    """
    rebuilt = {**PAYLOAD, "veyra_proof/cli.py": b"# the tool, from another commit\n"}
    (dist / "dist" / WHEEL).write_bytes(_zip(rebuilt, when=1))
    index = Index(files={f"/files/{WHEEL}": _zip(rebuilt, when=2)})
    index.release = index.release_payload([WHEEL])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "built from a commit other than the one" in out
    assert "veyra_proof/cli.py is not what the released commit" in out
    entry = _facts(report)["facts"]["files"][0]
    assert entry["identical_members"] == len(PAYLOAD), "the payload comparison is clean"
    assert entry["provenance"]["differing_members"] == ["veyra_proof/cli.py"]
    assert entry["provenance"]["proven"] is False


def test_a_member_the_released_commit_does_not_have_fails(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file nobody reviewed is worse than a changed one, and says so."""
    extra = {**PAYLOAD, "veyra_proof/extra.py": b"# nobody checked this\n"}
    (dist / "dist" / WHEEL).write_bytes(_zip(extra, when=1))
    index = Index(files={f"/files/{WHEEL}": _zip(extra, when=2)})
    index.release = index.release_payload([WHEEL])
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "veyra_proof/extra.py is in the published file and not in the released commit" in out


def test_a_package_file_the_published_files_do_not_ship_fails(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stale build: every member it has is the release's own — it just has fewer.

    Built before the release added the module, and matched member for member on
    everything it did carry. Only the other direction catches it: what the commit has
    must be shipped.
    """
    (dist / "src" / "veyra_proof" / "new.py").write_bytes(b"# added for the release\n")
    _commit(dist, "add the module")
    index = Index(
        files={
            f"/files/{SDIST}": _tar(SDIST_PAYLOAD, when=2),
            f"/files/{WHEEL}": _zip(PAYLOAD, when=2),
        }
    )
    index.release = index.release_payload([SDIST, WHEEL])
    try:
        code, _ = _run(check, dist, index.host)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "src/veyra_proof/new.py is in the released commit" in out
    assert "and not in the published file" in out
    assert "a stale build from a commit other than the one being released" in out


def test_the_tag_decides_which_commit_is_being_released(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The release is the tag's commit, not wherever the tree has wandered since.

    Everything here agrees — this checkout built and checked the very files the index
    serves — and it is still a failure, because they were built from a commit after the
    one the tag released. Payload equality cannot see that; pinning the comparison to
    the tag is what does.
    """
    subprocess.run(["git", "tag", f"v{VERSION}"], cwd=dist, check=True, capture_output=True)
    (dist / "src" / "veyra_proof" / "cli.py").write_bytes(b"# the tool, later\n")
    _commit(dist, "move on")
    later = {**PAYLOAD, "veyra_proof/cli.py": b"# the tool, later\n"}
    (dist / "dist" / WHEEL).write_bytes(_zip(later, when=1))
    index = Index(files={f"/files/{WHEEL}": _zip(later, when=2)})
    index.release = index.release_payload([WHEEL])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, report=report)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "built from a commit other than the one" in out
    facts = _facts(report)["facts"]
    assert facts["released_from"] == f"the tag v{VERSION}"
    assert facts["files"][0]["provenance"]["differing_members"] == ["veyra_proof/cli.py"]


def test_commit_names_the_commit_being_released(
    check: ModuleType, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--commit` is for the caller who already knows — and an unnameable one is misuse,
    not a verdict."""
    subprocess.run(["git", "tag", f"v{VERSION}"], cwd=dist, check=True, capture_output=True)
    (dist / "src" / "veyra_proof" / "cli.py").write_bytes(b"# the tool, later\n")
    _commit(dist, "move on")
    index = Index(files={f"/files/{WHEEL}": _zip(PAYLOAD, when=2)})
    index.release = index.release_payload([WHEEL])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, dist, index.host, "--commit", "HEAD~1", report=report)
    finally:
        index.close()

    out = capsys.readouterr().out
    assert code == 0, out
    seed = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=dist,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert _facts(report)["facts"]["released_from"] == "named by --commit HEAD~1"
    assert _facts(report)["facts"]["released_commit"] == seed

    index = Index(files={f"/files/{WHEEL}": _zip(PAYLOAD, when=2)})
    index.release = index.release_payload([WHEEL])
    try:
        code, _ = _run(check, dist, index.host, "--commit", "not-a-ref")
    finally:
        index.close()

    assert code == 2
    assert "names no commit" in capsys.readouterr().err


def test_when_git_cannot_say_which_commit_the_step_is_unproven(
    check: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A matching payload is not a provenance.

    Without a commit to compare against the question cannot be answered, and an
    unanswered question is not a green check — the policy `platforms` applies to CI.
    """
    root = tmp_path / "loose"
    (root / "dist").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{DIST_NAME}"\nversion = "{VERSION}"\n', encoding="utf-8"
    )
    (root / "dist" / WHEEL).write_bytes(_zip(PAYLOAD, when=1))
    (root / "dist" / SDIST).write_bytes(_tar(SDIST_PAYLOAD, when=1))
    index = Index(
        files={
            f"/files/{WHEEL}": _zip(PAYLOAD, when=2),
            f"/files/{SDIST}": _tar(SDIST_PAYLOAD, when=2),
        }
    )
    index.release = index.release_payload([WHEEL, SDIST])
    report = tmp_path / "published.json"
    try:
        code, _ = _run(check, root, index.host, report=report)
    finally:
        index.close()

    err = capsys.readouterr().err
    assert code == 2
    assert "provenance cannot be established" in err
    assert "unproven rather than assumed" in err
    assert _facts(report)["ok"] is False
