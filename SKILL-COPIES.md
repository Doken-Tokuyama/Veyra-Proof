# The copies of the `veyra-proof` skill

Four files with the same name can sit on one machine at once, and only one of them
belongs to this repository. This guide says which one you are looking at, who owns
the write, and what to do when it disagrees with the repo.

Read this before you edit anything under a `skills/` directory.

## The four copies

| Copy | Where it is | Who writes it | Whose it is | Edit it here? | Watched by | Put right by |
|---|---|---|---|---|---|---|
| **canonical** | `.cursor/skills/veyra-proof/` in this checkout | you | this repository | **yes — the only one** | `pytest` | your editor |
| **harness fan-out** | `~/.agents/skills/`, `~/.cursor/skills/` and seven more | `scripts/sync_skill.py` | this repository, materialized on your machine | no | `check_skills.py` — **fails** | `sync_skill.py` |
| **Cloud account copy** | `~/.config/freebuff-desktop/account-skills/<account>/<revision>/` | the Freebuff Desktop app | the app, from your Cloud snapshot | **no — overwritten in about a second** | `check_account_skill.py` — **warns** | the app's skill manager, by hand |
| **published copy** | `Veyra/skills/veyra-proof/` in this checkout | you, in lockstep with canonical | this repository | no — regenerate it from canonical | `pytest` | copy from canonical |

## Which one am I looking at?

Ask in this order; the first answer that fits is the copy:

1. **Does the directory contain a `.veyra-skill-sync` file?** → harness fan-out.
   That marker is this repo's ownership stamp, and only `sync_skill.py` writes it.
2. **Is the path under `~/.config/freebuff-desktop/account-skills/`?** → the Cloud
   account copy. It is a cache owned by the app: read it, never edit it.
3. **Is it under `.cursor/skills/veyra-proof/` inside this checkout?** → canonical.
   This is yours.
4. **Is it under `Veyra/skills/veyra-proof/`?** → the published copy. If it differs
   from canonical, the suite is already failing.
5. **None of the above?** → not ours. A skills directory is shared with every other
   skill on the machine, so nothing here touches a copy it did not stamp.

The frontmatter `metadata.version` tells you which release a copy came from, which
is usually enough to read a discrepancy without any of the scripts: a copy whose
version is behind the repo is *older*, not *edited*. The repo's version is
`pyproject.toml`'s, and `tests/test_docs_match_code.py` keeps them equal.

## 1. Canonical — the copy you edit

- `SKILL.md` is loaded into the agent's context on every trigger, so it stays lean;
  `reference.md` holds lookup material that only gets read when needed.
- Everyone else is compared against this directory by SHA-256, so a fix that lands
  only here is a fix that has not landed yet.
- Never edit inside `HARD_RULES_START` / `HARD_RULES_END`; keep the frontmatter
  version equal to `pyproject.toml` and `src/veyra_proof/__init__.py`; stay
  harness-neutral (no machine paths, no vendor CLI names).
- Maintainer-facing material — this file, the portability tables, the sync
  commands — lives in the README and here, never in the skill.
- After any edit: `python3 -m pytest`, then `python3 scripts/check_skills.py`. If
  the fan-out drifted, repair it with `python3 scripts/sync_skill.py`.

## 2. Harness fan-out — the machine's copies

`scripts/sync_skill.py` materializes the canonical skill into every harness it
detects. A harness counts as installed when its home directory exists; `--all`
overrides that. Global targets, in the order they are considered:

| Harness | Global target |
|---|---|
| Agent Skills (universal) | `~/.agents/skills` |
| Cursor | `~/.cursor/skills` |
| Claude Code | `~/.claude/skills` |
| Codex | `~/.codex/skills` |
| Antigravity | `~/.gemini/config/skills` |
| Freebuff | `~/.config/manicode/.agents/skills` |
| OpenCode | `~/.config/opencode/skills` |
| Cline | `~/.clinerules/skills` |
| Goose | `~/.goose/skills` |

Project-scoped targets (`.cursor/skills`, `.claude/skills`, `.agents/skills`,
`.github/skills`, `.windsurf/skills`, `.opencode/skills`, `.continue/skills`,
`.trae/skills`) are created only with `--project`. The README carries the full
table with both columns; `sync_skill.py` prints the paths it actually resolved, so
that output — not this list — is the authority for your machine.

Ownership is what makes repair safe:

- the write is atomic (temp file plus rename), so no reader sees half a file;
- copies are compared by SHA-256, never by mtime;
- every write is appended to `~/.local/share/veyra-skill-sync.log`;
- every copy the script writes carries the `.veyra-skill-sync` marker. Its filename
  deliberately does not contain the skill name, so the stamp survives the rename
  that would otherwise leave a live copy behind, still declaring the old name.
  `--prune` reports those copies and `--prune --apply` deletes them — stamped
  directories only.

`python3 scripts/sync_skill.py --check` reports drift and exits 1 without writing.
Its report places itself even when this guide is not open: it names the copy it is
reporting — `copy  harness fan-out — owned by this repository` — marks the canonical
copy as the one you edit, and closes by naming the two copies it does not own, the
Cloud account copy and the published copy, with the owner of each. Silence about a
copy should never read as a pass, so the legend prints on every run, including the
one that finds no harness at all. `check_skills.py` runs that check and treats drift
there as a **failure**, because this repository can repair it.

Why `~/.agents/skills` matters more than the rest: on its sync, the Freebuff
Desktop app imports local skills from `~/.claude/skills` and then `~/.agents/skills`,
so the second one wins. A stale copy there is the copy that gets imported — which
is the drift that reaches the Cloud account copy below.

## 3. The Cloud account copy — not ours

Freebuff Desktop keeps its own copy of every account skill under
`~/.config/freebuff-desktop/account-skills/<account>/<revision>/`, and that store is
deliberately **not** a fan-out target. Two things in it are compared:

- `current.json` — the cached Cloud snapshot. This is the one that decides what the
  next sync serves to every machine, including everyone else's.
- each materialized `veyra-proof` directory — a projection of that snapshot, and the
  file an agent actually reads today.

The app re-materializes both from Cloud on every sync, so a local edit there is
discarded within about a second. That is why it is **checked and never synced**:
writing to it would be a lie that lasts a second. Nothing in this repository can
repair it either — the remedy is a click in the app's skill manager, which removes
the stale entry so the next sync re-imports the corrected local copy.

It lags for a reason the fan-out cannot see: the app adds a skill to Cloud only when
its name is *missing* from the snapshot. `veyra-proof` is already there, so a newer
local copy is never re-imported and Cloud keeps the text it was first imported with —
while every harness copy matches and `sync_skill.py --check` stays green.

How to read the report: `current` matches the repo, `OLDER` declares a lower version
than the repo, `DIFFERS` differs at the same or an unknown version, `absent` means
this skill is not in that snapshot. A machine with no store at all exits 0: absence
is not drift. Exit 2 means the checker itself could not run, which is a defect here
and not a stale cache.

**Policy, and why it is not a failure:** the account copy **warns and passes**. It is
machine-local, it does not ship with a release, and this repository cannot repair
it — failing on it would mean a contributor whose Cloud cache is behind could never
get a green check, and a check that can never go green is one people learn to ignore.
A sub-check that cannot run at all still fails, because that is a defect in the
tooling rather than an out-of-date copy.

Treat this copy as a **cached older release**. It is not a source of truth for the
skill, and it is not the place to fix anything.

## 4. Published copy — the one the archive carries

`Veyra/skills/veyra-proof/` is a standalone, publishable copy of the skill, with its
own standard-library-only `install.py` for people who clone the repo or install from
the sdist. `MANIFEST.in` lists it, so the sdist carries the skill itself.

It is a byte-for-byte projection of canonical, and
`tests/test_skill_document.py::test_published_skill_is_byte_identical_to_canonical`
fails the moment the two disagree. It is never a fan-out target and never flows
back: when you edit canonical, copy the file across in the same change.

## Commands, and what each exit code means

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `python3 -m pytest` | every guard holds | a guard failed | — |
| `python3 scripts/check_skills.py` | fan-out in sync (account lag warns) | harness copies drifted → `sync_skill.py` | a checker could not run |
| `python3 scripts/sync_skill.py --check` | all targets in sync | stale copies, nothing written | usage error, or no canonical skill |
| `python3 scripts/check_account_skill.py` | account copy current, or no store | account copy lags | usage error |
| `python3 scripts/pre_release.py --tag vX.Y.Z --title "..."` | tagged (annotated, `vX.Y.Z — title`) | the release was refused | misuse |

The release path runs the suite, the copy checks, a build validated by `twine check`,
and a real audit, then tags — it refuses a skill copy that disagrees with the repo,
including a *tracked* skill file left uncommitted, which is the case that actually
decides what ships, since `publish.yml` builds the sdist from the tagged commit.

## What none of this can do

- **`git tag` alone bypasses the release gate.** Git has no pre-tag hook; the gate is
  real only for people who tag through `pre_release.py`. Anyone can bypass it, and
  the honest position is to say so rather than imply enforcement git cannot provide.
- **Nothing here repairs the Cloud snapshot.** The app's skill manager is the only
  lever, and it is a click, not a command.
- **The fan-out only reaches harnesses it can see.** A harness with no home
  directory gets nothing unless you pass `--all`, and in-repo copies need
  `--project`. Which copies exist is a property of your machine.
- **A skills directory is shared.** Only directories carrying the
  `.veyra-skill-sync` marker are ever pruned; anything else is left alone, reported
  at most.
- **`--check` never stamps.** A copy installed before this marker existed stays
  unstamped until a real sync writes it, so `--prune` cannot see it yet.

## How this guide stays true

`tests/test_skill_copies_guide.py` pins it to the code: the canonical path, the
marker filename and the account store path must be the ones the scripts use, every
global fan-out target must appear, the account policy must be stated as warn-not-fail,
the limit of the release gate must be stated, and every script this guide tells you to
run must exist. A harness added to `sync_skill.py` fails that test until it is listed
here.

The guide is listed in `MANIFEST.in`, so someone who only has the sdist — which
contains three of these four copies — can still tell them apart.
