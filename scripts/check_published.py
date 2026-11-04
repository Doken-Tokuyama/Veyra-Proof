#!/usr/bin/env python3
"""Ask the index what it serves, and compare it with the artifact this gate checked.

Every other step of the checklist looks at this checkout. This one looks at the thing a
user installs *instead* of it, and it exists because of what cutting v0.5.0 taught by
being the first release of the new gate: the comparison was made by hand, once, in a
terminal — the wheel in `dist/` against the wheel PyPI served, 44 members each, identical
— and had that come out differently, or had nobody thought to look, the checklist would
have said PASS about a file nobody installs. `publish.yml` builds its own sdist and wheel
from the tagged commit and uploads *those*, so what the index serves is a second build of
the same commit that no step had ever opened.

What is compared is the payload, not the file: the set of member names, and the sha256 of
each member's content. Wheels and sdists are not byte-reproducible by default — a zip
stores each member's timestamp, gzip stores one of its own — so two builds of one commit
give different bytes and identical payloads. Byte-identity is therefore not the claim
here, and the report says so in place of implying it.

Provenance is the second half of the claim, and it is not payload equality. "The files
match what we built" says nothing about *which commit* the world is installing, and a
release is a claim about a commit. PyPI can carry that answer in a file's attestation
(``provenance.attestation_url`` in the JSON API) — but this project's uploads have none,
and the artifacts themselves name no commit, so the only proof left is content: every
member the index serves has to be exactly what the released commit holds. That is what
this step checks, by reading the commit's own blobs with ``git cat-file``:

* **every member that is not build-generated** — the source, the tests, the workflows,
  the skill — must exist in the released commit **byte for byte**;
* **a member the released commit does not have at all** fails as ``not in the released
  commit``, because a file nobody reviewed is worse than a changed one;
* **every file the commit tracks under** ``src/`` **must ship** in the published file —
  which is what catches the stale build: an artifact made from an older commit can match
  the release in every member it *has*, while silently missing the ones added since.

"The released commit" is the one being cut: the ``v<version>`` tag's commit when the tag
exists, else this checkout's HEAD — and ``--commit`` names it explicitly for callers who
know better. Pinning it to the tag rather than to wherever the tree is now is what lets a
run on a moved tree still *prove* the published files are the release's own; the payload
difference that remains there is then described for what it is.

The one thing content cannot distinguish, stated plainly: a commit that changes nothing
any artifact carries (a `.gitignore` line, this repository's local `AGENTS.md`) produces
files identical to the release's, and no check of bytes could tell them apart. What is
proven is that users install the released commit's code; provenance beyond that needs the
attestation this project does not yet publish.

Three answers, and the middle one is the usual one at tag time:

* **the index has no files for this version** — publishing happens *after* the gate, so
  this is expected. It passes, and the facts record ``published: false``, so the release
  notes say "nothing yet" rather than carrying a row that reads like a verification;
* **the index serves this version and every payload matches** — it passes, and the facts
  name how many members were compared, per file;
* **a payload differs, or the index serves a file this checkout cannot show** — it fails,
  naming what differs, because the alternative is a release note claiming the index serves
  what the gate checked when it does not.

Exit 0 when the index agrees (or has nothing yet), 1 when it disagrees, 2 on misuse or
when the index cannot be asked at all: an unverifiable release is left unproven rather
than assumed green, which is the same policy `check_platforms.py` applies to CI.

Because the answer lives outside this checkout, this step is **never resumed**: the index
changes while the tree does not, so a record from an earlier run would say "not published
yet" about a version that has since been published. It is cheap — one JSON request and
two downloads — so it is simply asked again.

Usage::

    python3 scripts/check_published.py
    python3 scripts/check_published.py --index-url https://test.pypi.org/simple
    python3 scripts/check_published.py --report .git/veyra-release-reports/published.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_INDEX = "https://pypi.org"
TIMEOUT_S = 30.0

# Identifies this check to the index rather than arriving as an anonymous default agent,
# which is what the JSON API's own guidance asks for.
USER_AGENT = "veyra-proof-release-check"

WHEEL = "wheel"
SDIST = "sdist"


class IndexUnreachable(Exception):
    """The index could not be asked. That is not a verdict about the release."""


def json_host(index_url: str) -> str:
    """The host part of an index URL, from either spelling of it.

    pip calls the project index `…/simple`; the JSON API hangs off the host. Accepting
    both means `--index-url https://test.pypi.org/simple` — the string somebody has in
    their pip config — works instead of building `…/simple/pypi/…`.
    """
    trimmed = index_url.rstrip("/")
    if trimmed.endswith("/simple"):
        trimmed = trimmed[: -len("/simple")]
    return trimmed


def release_url(index: str, name: str, version: str) -> str:
    return f"{index}/pypi/{name}/{version}/json"


def project_metadata(root: Path) -> tuple[str, str]:
    """The name and version `pyproject.toml` declares — the release being cut."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    return project["name"], project["version"]


def fetch_json(url: str, timeout: float) -> dict | None:
    """The release's JSON, or None when the index has no such release (404)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise IndexUnreachable(f"{url} answered HTTP {error.code} {error.reason}") from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise IndexUnreachable(f"cannot read {url}: {error}") from error


def download(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise IndexUnreachable(f"cannot download {url}: {error}") from error


def served_files(payload: dict) -> list[dict]:
    """The files the index lists for this release, with what it says about each."""
    files = payload.get("urls")
    if not isinstance(files, list):
        return []
    served: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict) or not entry.get("filename"):
            continue
        digests = entry.get("digests")
        served.append(
            {
                "filename": str(entry["filename"]),
                "url": str(entry.get("url") or ""),
                "sha256": (digests or {}).get("sha256") if isinstance(digests, dict) else None,
                "yanked": bool(entry.get("yanked")),
            }
        )
    return served


def kind_of(filename: str) -> str:
    """Which archive this is, from its name: the two shapes `build` produces."""
    if filename.endswith(".whl"):
        return WHEEL
    if filename.endswith((".tar.gz", ".tgz")):
        return SDIST
    return "unknown"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def member_digests(data: bytes, kind: str) -> dict[str, str]:
    """Every member of an archive, by name, with the sha256 of its content.

    Members rather than the archive, because the two builds being compared are not
    byte-reproducible and the payload is what the claim is about. Both shapes go through
    the same rule, so a wheel and an sdist are compared the same way.
    """
    if kind == WHEEL:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {
                name: sha256_bytes(archive.read(name))
                for name in archive.namelist()
                if not name.endswith("/")
            }
    if kind == SDIST:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            return {
                member.name: sha256_bytes(archive.extractfile(member).read())  # type: ignore[union-attr]
                for member in archive.getmembers()
                if member.isfile()
            }
    raise ValueError(f"not a wheel or an sdist: {kind}")


def compare(served_entry: dict, served_bytes: bytes, local: Path) -> tuple[dict, list[str]]:
    """One served file against its namesake in this checkout. Returns (facts, problems)."""
    filename = served_entry["filename"]
    kind = kind_of(filename)
    record: dict = {
        "filename": filename,
        "kind": kind,
        "yanked": served_entry["yanked"],
        # Measured from the bytes that arrived, not quoted from the index's own claim
        # about them: the claim is checked against the download below, and a digest this
        # script repeated rather than computed could agree with a file nobody has.
        "served_sha256": sha256_bytes(served_bytes),
        "declared_sha256": served_entry["sha256"],
        "members": 0,
        "identical_members": 0,
        "differing_members": [],
        "archive_identical": False,
        "missing_local": False,
        "unreadable": None,
    }
    problems: list[str] = []

    if not local.is_file():
        # The name is the pairing rule: the file the index serves has to be the file the
        # gate built and installed here. A rebuild that tagged itself differently is a
        # different artifact, whatever it contains.
        record["missing_local"] = True
        record["local_sha256"] = None
        problems.append(
            f"the index serves {filename}, and there is no file of that name in this "
            f"checkout's dist/ — the artifact the gate checked is not the one users get"
        )
        return record, problems

    local_bytes = local.read_bytes()
    record["local_sha256"] = sha256_bytes(local_bytes)
    record["archive_identical"] = record["local_sha256"] == record["served_sha256"]

    declared = served_entry["sha256"]
    if declared and declared != record["served_sha256"]:
        problems.append(
            f"the index's own digest for {filename} does not describe the file it served"
        )

    try:
        served_members = member_digests(served_bytes, kind)
        local_members = member_digests(local_bytes, kind)
    except (ValueError, zipfile.BadZipFile, tarfile.TarError, OSError) as error:
        record["unreadable"] = str(error)
        problems.append(f"{filename} cannot be read as a {kind}: {error}")
        return record, problems

    shared = set(served_members) & set(local_members)
    differing = sorted(name for name in shared if served_members[name] != local_members[name])
    only_served = sorted(set(served_members) - set(local_members))
    only_local = sorted(set(local_members) - set(served_members))

    record["members"] = len(served_members)
    record["identical_members"] = len(shared) - len(differing)
    record["differing_members"] = (
        differing
        + [f"{name} (served only)" for name in only_served]
        + [f"{name} (this checkout only)" for name in only_local]
    )

    for name in differing[:3]:
        problems.append(f"{filename}: {name} differs between what the index serves and dist/")
    for name in only_served[:3]:
        problems.append(f"{filename}: {name} is in the file the index serves and not in dist/")
    for name in only_local[:3]:
        problems.append(f"{filename}: {name} is in dist/ and not in the file the index serves")
    if len(record["differing_members"]) > 3:
        problems.append(
            f"{filename}: {len(record['differing_members'])} members differ in total"
        )
    return record, problems


def _git(root: Path, *argv: str) -> str | None:
    """git, or None when it cannot answer. Never fatal: this is a diagnosis."""
    try:
        completed = subprocess.run(
            ["git", *argv], cwd=root, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def released_tree(root: Path, version: str) -> tuple[str | None, str | None]:
    """(the commit the ``v<version>`` tag names, this checkout's HEAD), when git can say.

    A mismatch explains a failure a difference count cannot: the artifacts this checkout
    builds are not the ones that were published, and no rebuild here would change that.
    """
    return _git(root, "rev-parse", f"v{version}^{{commit}}"), _git(root, "rev-parse", "HEAD")


def released_commit(root: Path, version: str, override: str | None) -> tuple[str | None, str]:
    """(the commit being released, how it was named).

    The tag is the release: it exists exactly to say which commit the world got, and it
    keeps saying so after this checkout has moved on. Before the tag exists — the gate
    runs before it creates one — the release is being cut from HEAD. ``--commit`` settles
    it for a caller who already knows.
    """
    if override:
        return _git(root, "rev-parse", f"{override}^{{commit}}"), f"named by --commit {override}"
    tag, head = released_tree(root, version)
    if tag:
        return tag, f"the tag v{version}"
    return head, "HEAD"


def commit_tree(root: Path, commit: str) -> dict[str, str] | None:
    """The commit's files as ``path -> sha256``, or None when git cannot show them.

    Read from the commit's own blobs, not from ``git archive``. Archive output is not
    the blob: with ``core.autocrlf=true`` — what GitHub's Windows runners set — git
    rewrites line endings on the way out, so every text member differed from a wheel
    built from the committed bytes. Run 41882003503 is the measurement: all three
    Windows jobs reported both members as "not what the released commit holds" while
    every other platform was green, over an artifact that matched its commit exactly.
    ``cat-file`` has no layer between the object and the digest, which is the thing
    this step claims to compare; a checkout's filters cannot reach it either.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--full-tree", commit],
            cwd=root,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None

    blobs: list[tuple[str, str]] = []
    for record in listing.stdout.split(b"\0"):
        meta, _, path = record.partition(b"\t")
        if not path:
            continue
        fields = meta.split()
        if len(fields) < 3 or fields[1] != b"blob":
            continue
        blobs.append((fields[2].decode("ascii"), path.decode("utf-8", "surrogateescape")))
    if not blobs:
        return None

    try:
        described = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=root,
            input=b"".join(sha.encode("ascii") + b"\n" for sha, _ in blobs),
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if described.returncode != 0:
        return None

    data = described.stdout
    offset = 0
    tree: dict[str, str] = {}
    for _, blob_path in blobs:
        header_end = data.find(b"\n", offset)
        if header_end < 0:
            return None
        fields = data[offset:header_end].split()
        if len(fields) < 3 or fields[1] != b"blob":
            return None
        size = int(fields[2])
        start = header_end + 1
        tree[blob_path] = sha256_bytes(data[start : start + size])
        # `cat-file --batch` prints the blob, then one newline before the next header.
        offset = start + size + 1
    return tree


GENERATED_BASENAMES = {"PKG-INFO", "setup.cfg"}
PACKAGE_SOURCE = "src/"


def generated_member(path: str) -> bool:
    """A file a build backend writes into the artifact, describing the build.

    ``PKG-INFO`` and the ``[egg_info]`` ``setup.cfg`` land in the sdist, ``*.egg-info/*``
    beside it, ``*.dist-info/*`` in the wheel; none of them is source and none of them is
    in any commit. Consulted only for a member with no counterpart in the tree, so a
    commit that *does* carry one of these names is still compared like any other file.
    """
    if path.rsplit("/", 1)[-1] in GENERATED_BASENAMES:
        return True
    return any(
        segment.endswith((".egg-info", ".dist-info")) for segment in path.split("/")
    )


def source_candidates(member: str, kind: str) -> list[str]:
    """Where in the commit this member came from. An sdist is the tree plus its root
    directory; a wheel is the package, laid out as the repository lays it out.
    """
    if kind == SDIST:
        stripped = member.split("/", 1)[1] if "/" in member else member
        return [stripped]
    return [PACKAGE_SOURCE + member, member]


def provenance(
    served: dict[str, str], kind: str, tree: dict[str, str], label: str
) -> tuple[dict, list[str]]:
    """Whether this published file is the released commit's, and how it is not.

    `served` maps member names to the sha256 of their content, `tree` is the released
    commit's files the same way. Every member must be the commit's own file; every file
    the commit keeps under `src/` must be shipped. The one-directional check is not
    enough in either direction: a member comparison alone clears an artifact built from
    an older commit (its members are all real, just fewer), and a shipped-file comparison
    alone clears one carrying files no reviewer ever saw.
    """
    matched = 0
    generated: list[str] = []
    differing: list[str] = []
    not_in_commit: list[str] = []
    missing_source: list[str] = []
    problems: list[str] = []

    for member in sorted(served):
        hit = next(
            (path for path in source_candidates(member, kind) if path in tree), None
        )
        if hit is None:
            if generated_member(member):
                generated.append(member)
            else:
                not_in_commit.append(member)
                problems.append(
                    f"{member} is in the published file and not in {label} — it was "
                    "built from a commit other than the one being released"
                )
            continue
        if tree[hit] == served[member]:
            matched += 1
        else:
            differing.append(member)
            problems.append(
                f"{member} is not what {label} holds — the published file was built "
                "from a commit other than the one being released"
            )

    for path in sorted(tree):
        if not path.startswith(PACKAGE_SOURCE):
            continue
        if kind == SDIST:
            shipped = any(
                name == path or name.endswith("/" + path) for name in served
            )
        else:
            shipped = path[len(PACKAGE_SOURCE) :] in served
        if not shipped:
            missing_source.append(path)
            problems.append(
                f"{path} is in {label} and not in the published file — a stale build "
                "from a commit other than the one being released"
            )

    facts = {
        "members_matched": matched,
        "generated_members": generated,
        "differing_members": differing,
        "not_in_commit": not_in_commit,
        "missing_source": missing_source,
        "proven": not problems,
    }
    return facts, problems


def write_report(path: Path | None, facts: dict, ok: bool) -> None:
    """State the measurement as JSON, so a release note never restates it by hand."""
    if path is None:
        return
    payload = {"tool": "check_published", "ok": ok, "facts": facts}
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
            "Compare the artifacts the package index serves with the ones this checkout "
            "built, member by member."
        )
    )
    parser.add_argument("--root", type=Path, default=None, help="Checkout to release.")
    parser.add_argument(
        "--dist",
        type=Path,
        default=None,
        help="Where the built artifacts are (default: <root>/dist).",
    )
    parser.add_argument(
        "--index-url",
        default=DEFAULT_INDEX,
        help=(
            "Index to ask, as a host (default: %(default)s) or the way pip spells it "
            "(`…/simple`); the JSON API is used at <index>/pypi/<name>/<version>/json."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=TIMEOUT_S,
        help="Seconds for each request to the index (default: %(default)s).",
    )
    parser.add_argument(
        "--commit",
        default=None,
        help=(
            "The commit being released (default: the `v<version>` tag's commit, else "
            "this checkout's HEAD) — what the published files have to be built from."
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write what was measured, as JSON, to this path (used by the gate).",
    )
    args = parser.parse_args(argv)
    facts: dict[str, Any]

    root = (args.root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    dist = (args.dist or root / "dist").expanduser().resolve()
    index = json_host(args.index_url)

    try:
        name, version = project_metadata(root)
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        print(f"cannot read the project name and version from {root}: {error}", file=sys.stderr)
        return 2

    url = release_url(index, name, version)
    print(f"index      {index}")
    print(f"project    {name} {version}")
    print(f"built      {dist}")
    print(f"asking     {url}")
    print()

    try:
        payload = fetch_json(url, args.timeout)
    except IndexUnreachable as error:
        facts = {"version": version, "index": index, "published": None, "error": str(error)}
        write_report(args.report, facts, ok=False)
        print(f"  FAIL  {error}", file=sys.stderr)
        print(
            "        The index could not be asked, so whether users get what this gate "
            "checked is unknown — left unproven rather than assumed.",
            file=sys.stderr,
        )
        return 2

    if payload is None:
        facts = {
            "version": version,
            "index": index,
            "published": False,
            "reason": "the index has no files for this version yet",
            "files": [],
            "files_compared": 0,
        }
        print(f"  none   {name} {version} is not on {index}: there is nothing to compare")
        print("         Publishing happens after the gate, so this is the normal answer")
        print("         at tag time. Run the gate again afterwards, or once the release")
        print("         is public: the comparison is made the moment there is a file to")
        print("         compare, and the notes then state which payloads matched.")
        write_report(args.report, facts, ok=True)
        return 0

    served = served_files(payload)
    if not served:
        facts = {
            "version": version,
            "index": index,
            "published": True,
            "reason": "the index lists this version with no files",
            "files": [],
            "files_compared": 0,
        }
        write_report(args.report, facts, ok=False)
        print(f"  FAIL  {index} lists {name} {version} with no files at all", file=sys.stderr)
        print("        A version with nothing to install is not a release.", file=sys.stderr)
        return 1

    problems: list[str] = []
    records: list[dict] = []
    digests_by_file: dict[str, dict[str, str]] = {}
    for entry in served:
        filename = entry["filename"]
        local = dist / filename
        if not entry["url"]:
            problems.append(f"the index gives no download URL for {filename}")
            print(f"  ?      {filename} — the index gives no download URL")
            continue
        try:
            served_bytes = download(entry["url"], args.timeout)
        except IndexUnreachable as error:
            facts = {"version": version, "index": index, "published": True, "error": str(error)}
            write_report(args.report, facts, ok=False)
            print(f"  FAIL  {error}", file=sys.stderr)
            return 2

        record, found = compare(entry, served_bytes, local)
        records.append(record)
        problems += found
        try:
            digests_by_file[filename] = member_digests(served_bytes, record["kind"])
        except (ValueError, zipfile.BadZipFile, tarfile.TarError, OSError):
            pass  # unreadable: `compare` already turned that into a failure

        if record["missing_local"]:
            print(f"  FAIL   {filename} — nothing in dist/ to compare it with")
        elif record["unreadable"]:
            print(f"  FAIL   {filename} — {record['unreadable']}")
        elif found:
            print(
                f"  FAIL   {filename} — {record['identical_members']} of "
                f"{record['members']} members identical"
            )
            for detail in found[:3]:
                print(f"         {detail}")
        else:
            note = ""
            if record["yanked"]:
                note = " (yanked: pip will not install it by default)"
            elif record["archive_identical"]:
                note = " (the archives match byte for byte as well)"
            print(
                f"  ok     {filename} — {record['identical_members']} of "
                f"{record['members']} members identical{note}"
            )

    yanked = [record["filename"] for record in records if record["yanked"]]
    if yanked:
        print()
        print(f"  note   yanked on the index: {', '.join(yanked)}")

    commit, origin = released_commit(root, version, args.commit)
    if args.commit and not commit:
        print(f"  FAIL  --commit {args.commit} names no commit git can show", file=sys.stderr)
        return 2
    tree = commit_tree(root, commit) if commit else None
    commit_short = commit[:12] if commit else "unknown"
    label = f"the released commit {commit_short}" if commit else "the released commit"
    unproven = tree is None
    provenance_problems: list[str] = []
    if tree is not None:
        for record in records:
            digests = digests_by_file.get(record["filename"])
            if digests is None:
                continue  # unreadable: already a failure, and nothing here to prove
            facts_p, found_p = provenance(digests, record["kind"], tree, label)
            record["provenance"] = facts_p
            provenance_problems += found_p

    facts = {
        "version": version,
        "index": index,
        "published": True,
        "reason": "the index serves this version",
        "released_commit": commit,
        # Recorded, not derived later: the release notes cite this form, and every
        # number in them has to be one the record holds (release_notes.py's own guard).
        "released_commit_short": commit_short if commit else None,
        "released_from": origin,
        "files": records,
        "files_compared": len(records),
    }
    write_report(
        args.report,
        facts,
        ok=not problems and not provenance_problems and not unproven,
    )

    print()
    print("=== verdict")
    if problems or provenance_problems or unproven:
        if problems:
            print(f"  FAIL  {len(problems)} difference(s) between what the index serves for")
            print(f"        {version} and what this checkout builds.")
            tag, head = released_tree(root, version)
            if tag and head and tag != head:
                # The likeliest reading, and one that changes what the reader should do: the
                # version is already out, this tree is not the tree it came from, and the
                # artifacts here will never be the ones on the index.
                print(f"        v{version} names {tag[:12]} and this checkout is {head[:12]}, so the")
                print("        artifacts built here are not the published ones. Compare the release")
                print("        on its own tree (`git worktree add <dir> v" + version + "`), or bump the")
                print("        version and release the new one.")
            else:
                print("        Either the published artifact is not the one this gate checked, or")
                print("        the tree moved on after the release was published. Nothing here can")
                print("        repair either: a version cannot be re-uploaded to the index.")
            for problem in problems:
                print(f"        - {problem}")
        if provenance_problems:
            print("  FAIL  the index's files were built from a commit other than the one")
            print(f"        being released ({label}):")
            for problem in provenance_problems[:6]:
                print(f"        - {problem}")
            if len(provenance_problems) > 6:
                print(f"        - … {len(provenance_problems) - 6} more")
        elif unproven:
            # On stderr with the other "the check could not answer" diagnostics: this is
            # not a verdict about the release, it is the absence of one.
            print(
                "  FAIL  provenance cannot be established: git cannot name the commit "
                f"this release was cut from in {root}.\n"
                "        The payload comparison is reported above, but where the published\n"
                "        files came from is left unproven rather than assumed.",
                file=sys.stderr,
            )
        else:
            print(f"        provenance holds: every published file is exactly what {label}")
            print("        holds, so the published artifact is the release's own — this checkout")
            print("        is the one that is elsewhere.")
        return 2 if unproven and not problems and not provenance_problems else 1

    print("  PASS  every file the index serves has the payload this gate checked")
    print(
        f"        {len(records)} file(s), compared by member name and content: a rebuild "
        "is not byte-identical"
    )
    print("        unless it says so above, which is why the payload is what is compared.")
    print(f"  PASS  provenance: the published files are built from {commit_short}, the")
    print(f"        commit being released ({origin}): every member is what that commit")
    print("        holds, and every package file under `src/` it has is shipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
