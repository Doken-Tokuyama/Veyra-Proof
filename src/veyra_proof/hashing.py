"""File hashing and change detection."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from veyra_proof.models import FileHashSnapshot

TRACKED_RELATIVE_PATHS = (
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain.toml",
    "rust-toolchain",
    ".cargo/config.toml",
    "deny.toml",
    "supply-chain/config.toml",
    "veyra.toml",
)


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_path(root: Path, relative: str) -> FileHashSnapshot:
    path = root / relative
    exists = path.is_file()
    return FileHashSnapshot(
        path=str(path.resolve()) if exists or path.exists() else str(path),
        exists=exists,
        sha256=sha256_file(path) if exists else None,
    )


def snapshot_project_files(root: Path, extra: Iterable[str] = ()) -> dict[str, FileHashSnapshot]:
    rels = list(TRACKED_RELATIVE_PATHS) + list(extra)
    # Also hash all Cargo.toml under workspace members if present at root only by default.
    out: dict[str, FileHashSnapshot] = {}
    for rel in rels:
        out[rel] = snapshot_path(root, rel)
    return out


def compare_snapshots(
    before: dict[str, FileHashSnapshot],
    after: dict[str, FileHashSnapshot],
) -> list[dict[str, str | None]]:
    changes: list[dict[str, str | None]] = []
    keys = sorted(set(before) | set(after))
    for key in keys:
        b = before.get(key)
        a = after.get(key)
        b_hash = b.sha256 if b else None
        a_hash = a.sha256 if a else None
        b_exists = b.exists if b else False
        a_exists = a.exists if a else False
        if b_hash != a_hash or b_exists != a_exists:
            kind = "modified"
            if not b_exists and a_exists:
                kind = "created"
            elif b_exists and not a_exists:
                kind = "deleted"
            changes.append(
                {
                    "path": key,
                    "change": kind,
                    "before_sha256": b_hash,
                    "after_sha256": a_hash,
                }
            )
    return changes
