# The places the `veyra-proof` skill lives

Four files with the same name can sit on one machine at once, and only one of them
belongs to this repository. This guide says which one you are looking at, who owns
the write, what to do when it disagrees with the repo, and what the link between
them costs.

Read this before you edit anything under a `skills/` directory.

## The one you edit, and the three you don't

| Copy | Where it is | Who writes it | Whose it is | Edit it here? | Watched by | Put right by |
|---|---|---|---|---|---|---|
| **canonical** | `.cursor/skills/veyra-proof/` in this checkout | you | this repository | **yes — the only one** | `pytest` | your editor |
| **harness fan-out** | `~/.agents/skills/`, `~/.cursor/skills/` and seven more | `scripts/sync_skill.py` | this repository, linked into your machine | no — it *is* canonical | `check_skills.py` — **fails** if a target is not | `sync_skill.py` |
| **Cloud account copy** | `~/.config/freebuff-desktop/account-skills/<account>/<revision>/` | the Freebuff Desktop app | the app, from your Cloud snapshot | **no — overwritten in about a second** | `check_account_skill.py` — **warns** | the app's skill manager, by hand |
| **published copy** | `Veyra/skills/veyra-proof/` in this checkout | you, in lockstep with canonical | this repository | no — regenerate it from canonical | `pytest` | copy from canonical |

## Which one am I looking at?

Ask in this order; the first answer that fits is the one you are looking at:

1. **Is it a symlink?** → the harness fan-out, and the file you are reading *is*
   the canonical skill. `ls -l` shows the target; a key that no longer exists means
   this checkout moved (see “What the link costs”).
2. **Does the directory contain a `.veyra-skill-sync` file?** → harness fan-out in
   copy mode. That marker is this repo's ownership stamp, and only the `--copy`
   install writes it.
3. **Is the path under `~/.config/freebuff-desktop/account-skills/`?** → the Cloud
   account copy. It is a cache owned by the app: read it, never edit it.
4. **Is it under `.cursor/skills/veyra-proof/` inside this checkout?** → canonical.
   This is yours.
5. **Is it under `Veyra/skills/veyra-proof/`?** → the published copy. If it differs
   from canonical, the suite is already failing.
6. **None of the above?** → not ours. A skills directory is shared with every other
   skill on the machine, so nothing here touches a copy it did not install.

The frontmatter `metadata.version` tells you which release a copy came from, which
is usually enough to read a discrepancy without any of the scripts: a copy whose
version is behind the repo is *older*, not *edited*. The repo's version is
`pyproject.toml`'s, and `tests/test_docs_match_code.py` keeps them equal.

## 1. Canonical — the copy you edit

- `SKILL.md` is loaded into the agent's context on every trigger, so it stays lean;
  `reference.md` holds lookup material that only gets read when needed.
- The harness targets are **links to this directory**, so a fix here is live in
  every harness immediately. The Cloud account copy and the published copy are
  compared against it by SHA-256, so a fix that lands only here has not landed in
  those two yet.
- Never edit inside `HARD_RULES_START` / `HARD_RULES_END`; keep the frontmatter
  version equal to `pyproject.toml` and `src/veyra_proof/__init__.py`; stay
  harness-neutral (no machine paths, no vendor CLI names).
- Maintainer-facing material — this file, the portability tables, the sync
  commands — lives in the README and here, never in the skill.
- After any edit: `python3 -m pytest`, then `python3 scripts/check_skills.py`. That
  is the whole ritual now — with the fan-out linked, there is no copy of a fix left
  to propagate. Run `python3 scripts/sync_skill.py` after moving this checkout, or
  after installing a new harness.

## 2. Harness fan-out — the canonical skill, linked into your machine

`scripts/sync_skill.py` installs the canonical skill into every harness it
detects, as a **symlink** to `.cursor/skills/veyra-proof`. Nothing is duplicated,
so nothing can disagree: the harness reads the file you just saved, and the old
question — do the copies still match the repo? — stops having an answer because it
stops having a question. A harness counts as installed when its home directory
exists; `--all` overrides that. Global targets, in the order they are considered:

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

Ownership is what makes repair safe. A link needs no marker — its **target is the
proof**, and a stamp written inside a linked directory would land in the canonical
skill in this repo. So the rules are:

- a target is only ever replaced when it is a link, a stamped copy, or a directory
  holding nothing this skill does not ship. A directory with a file of its own in it
  is somebody's work: it is reported, it fails `--check`, and no write run touches
  it;
- with `--copy`, writes stay atomic (temp file plus rename) and copies carry the
  `.veyra-skill-sync` marker. Its filename deliberately does not contain the skill
  name, so the stamp survives the rename that would otherwise leave a live target
  behind still declaring the old name. The file holds two lines — the skill name,
  then the mode — so a stamp written by an *older* `sync_skill.py`, which recorded
  only the name, still proves the directory is ours for `--prune` but does **not**
  declare a copy: the default mode converts it to a link, which is how machines that
  already have copies move across;
- `--prune` deletes what it can prove it owns: a stamped directory, or a link whose
  target is inside this checkout's skills directory — which is also how it finds the
  link a rename left dangling;
- every install and every prune is appended to
  `~/.local/share/veyra-skill-sync.log`.

The report says which mode it is in (`mode  link` or `mode  copy`), and one word per
target: `ok`, `link`, `relink`, `copy`, or `skip`. `skip` is the refusal — the
target is not ours to replace — and it counts as a failure in `--check`, on purpose:
the one case where the script cannot help is the one a human has to see.

`python3 scripts/sync_skill.py --check` reports and exits 1 without writing.
Its report places itself even when this guide is not open: it names the fan-out it
reports — `copy  harness fan-out — owned by this repository; this script links it to
the canonical directory` — marks the canonical copy as the one you edit, and closes
by naming the two places it does not own, the Cloud account copy and the published
copy, with the owner of each. Silence about a place should never read as a pass, so
the legend prints on every run, including the one that finds no harness at all.
`check_skills.py` runs that check and treats a target that is not the canonical
skill as a **failure**, because this repository can repair it.

### What the link costs

This is the honest half, and it is not small:

- **A link is only as durable as this checkout.** Move, rename or delete the
  repository and every harness target goes dead — where a copy would have kept
  working. `--check` names that case separately ("the target does not exist: this
  checkout moved or was deleted") and `sync_skill.py` re-points it. A link that was
  left pointing at the *old* path is also one `--prune` cannot see, because the
  path it recorded is outside the checkout it is being run from: re-point the
  current name first, then delete the old one by hand.
- **It needs a filesystem that can hold a symlink.** Windows without Developer Mode
  refuses one, as do FAT and some SMB mounts. There the script falls back to a
  stamped copy — the install still works, but that machine is back to the mode that
  can drift, and the `mode` line says so rather than implying links are in place.
- **The link has to be followed by the harness.** The two local skill loaders in the
  shipped Freebuff Desktop app both use `statSync` on the skill directory, which
  resolves a link, so a linked skill is found and its `SKILL.md` read through it.
  The app's **Cloud-import** path could not be checked from here, so if you want to
  be conservative, keep `~/.claude/skills` and `~/.agents/skills` as copies: run
  `sync_skill.py --copy` and those two become stamped copies while the rest link.
- **One extra hop when reading.** A path in `~/.claude/skills/veyra-proof/` inside
  this repository resolves to `.cursor/skills/veyra-proof/`, so a symbol or file
  listed by a tool that does not resolve links can look like it lives outside the
  checkout.
- **`Veyra/install.py` still copies, on purpose.** The published repo is cloned by
  strangers who may delete the clone; linking their harness to a checkout they no
  longer have is worse than a copy they own.

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
while every harness target reads the canonical skill and `sync_skill.py --check`
stays green.

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
| `python3 scripts/check_skills.py` | every harness target is canonical (account lag warns) | a target is not → `sync_skill.py` | a checker could not run |
| `python3 scripts/sync_skill.py --check` | every target is canonical, or a deliberate copy | a target needs repair, nothing written | usage error, or no canonical skill |
| `python3 scripts/sync_skill.py --prune` | installs, then reports targets left by an older name | nothing stale to report | usage error |
| `python3 scripts/sync_skill.py --copy` | installs copies (and stamps them) instead of links | — | usage error |
| `python3 scripts/check_account_skill.py` | account copy current, or no store | account copy lags | usage error |
| `python3 scripts/pre_release.py --tag vX.Y.Z --title "..."` | tagged (annotated, `vX.Y.Z — title`) | the release was refused | misuse |
| `python3 scripts/check_wheel_version.py` | the built wheel installs and reports its own version | a wheel for another version, or one that disagrees with itself | misuse (no `pyproject.toml`) |
| `python3 scripts/check_platforms.py` | the commit's CI passed on Linux, macOS and Windows | a platform's job failed, was skipped or cancelled, or has no run yet | `gh` cannot read the run (not a pass) |
| `python3 scripts/release_notes.py --title "…"` | notes written from the gate's measurements | the record does not cover this tree, or there is nothing to release | misuse, or a tree git cannot fingerprint |

The release path runs the suite, that same suite on the other platforms as the CI
run for this commit actually ran it, the copy checks, a build validated by
`twine check`, the built wheel installed into a throwaway venv where it has to
report the version it was built as, a real audit, and the release notes generated
from what those steps measured, then tags — it refuses a
harness target that is not the canonical skill, a wheel that misnames itself, a
*platform the release has never run on* (0.4.3 was tagged and published while both
Windows jobs were failing), and a *tracked* skill file left uncommitted, which is
the case that actually decides what ships, since `publish.yml` builds the sdist
from the tagged commit.

## What none of this can do

- **`git tag` alone bypasses the release gate.** Git has no pre-tag hook; the gate is
  real only for people who tag through `pre_release.py`. Anyone can bypass it, and
  the honest position is to say so rather than imply enforcement git cannot provide.
- **The `platforms` step needs GitHub to be reachable and authenticated.** Without
  `gh` — or offline, or on a commit that has not been pushed — it exits 2 ("not run",
  which the gate treats as a refusal) rather than falling back to a local-only check.
  It reads the CI run for the *commit*, so a release is a push-CI-gate sequence, not
  a local one.
- **Nothing here repairs the Cloud snapshot.** The app's skill manager is the only
  lever, and it is a click, not a command.
- **The fan-out only reaches harnesses it can see.** A harness with no home
  directory gets nothing unless you pass `--all`, and in-repo targets need
  `--project`. Which places exist is a property of your machine.
- **A skills directory is shared.** Only what this script installed is ever
  replaced or pruned — a stamped directory, or a link into this checkout. Everything
  else is reported at most.
- **A link cannot be stamped, and a copy cannot be live.** The two modes trade
  drift for a dependency on the checkout, and neither is free. What the script will
  not do is let you believe you have both: the `mode` line says which one you are
  in, and `--check` accepts a copy only when it carries the marker that says a human
  chose copies.
- **`--check` never writes.** A target installed before this change is converted by
  a real run of `sync_skill.py`, not by a check.

## How this guide stays true

`tests/test_skill_copies_guide.py` pins it to the code: the canonical path, the
marker filename and the account store path must be the ones the scripts use, every
global fan-out target must appear, the account policy must be stated as warn-not-fail,
the limit of the release gate must be stated, every flag this guide tells you to pass
must appear in `sync_skill.py --help`, and every script it tells you to run must
exist. A harness added to `sync_skill.py` fails that test until it is listed here.

The same test file pins the costs above, so they cannot be quietly dropped when the
mode changes again: the two mode names, the sentence about a link being only as
durable as this checkout, the fallback for a filesystem without symlinks, the part of
the app that could not be checked from here, and the fact that `Veyra/install.py`
still copies.

The guide is listed in `MANIFEST.in`, so someone who only has the sdist can still
tell these places apart. That archive carries two of them — `.cursor/skills/veyra-proof/`
and `Veyra/skills/veyra-proof/` — plus this guide, the scripts and the suite. It
carries no harness fan-out, because a link to a checkout that is not on the
installing machine would mean nothing, and no account store, because that cache
belongs to the app.
