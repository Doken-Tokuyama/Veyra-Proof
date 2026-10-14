# Veyra Proof

[![CI](https://github.com/Doken-Tokuyama/Veyra-Proof/actions/workflows/ci.yml/badge.svg)](https://github.com/Doken-Tokuyama/Veyra-Proof/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/veyra-proof.svg)](https://pypi.org/project/veyra-proof/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**A Rust audit that tells you what it can prove — and what it can't.**

An audit that says "all clear" without evidence is a story, not a result. Veyra Proof runs the real cargo toolchain on your machine, prints the real exit codes, and writes every log, hash and skip-reason into `.veyra/evidence/<UTC>/`.

It does **not** prove your code is correct or free of vulnerabilities. A fuzz run with no crashes is not a security proof. Miri and Kani cover limited paths. Faster to know that, than to find out from a customer.

If a tool is missing it says `MISSING`. If it timed out it says `SKIPPED`. `OK` is never assigned to a check that did not run.

## Honest limitations

- This does **not** prove total correctness or absence of vulnerabilities.
- Anti-theater heuristics can produce false positives; they never declare "fake code confirmed."
- **Heuristics also do not see a placeholder that was written to look finished.** Real
  types, a doc comment citing a business rule, a plausible arithmetic body and tests that
  assert whatever the code currently does will pass `quick` with exit 0 — verified, with
  the crate in `tests/`, not as a guess. Nothing regex-shaped can separate that from real
  code. `veyra-e2e` is the layer that catches it, and only when the journeys are declared
  in `veyra.toml`. That is the single highest-value thing to wire up, and it is manual by
  design: inventing journeys nobody declared would produce a green audit for behaviour
  nobody checked.
- A fuzz run without crashes is **not** a security proof.
- Miri/Kani cover limited paths and properties.
- Tool availability varies by OS, architecture, toolchain, and project configuration.
- Diagnostic parsing is line-based. A tool whose failure message looks like another
  tool's finding can be mis-attributed, which is why a stale lockfile is classified
  before any output is parsed.

## Requirements

- Python 3.10+
- Rust/Cargo already installed (Veyra will **not** `curl | sh` install rustup)
- Optional: `rustup` for component installs (clippy, rustfmt, miri, llvm-tools)

## Install (local)

```bash
git clone https://github.com/Doken-Tokuyama/Veyra-Proof
cd Veyra-Proof
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`.

## Quick verification (demo fixture)

From the repo root:

```bash
cd Veyra-Proof
.venv/bin/python -m pytest
.venv/bin/python -m veyra_proof doctor
.venv/bin/python -m veyra_proof plan tests/fixtures/demo_crate --profile quick
.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate --profile quick
```

## Usage

Use the **demo fixture** included in this repo, or point at a **real** Rust workspace (a path that contains `Cargo.toml`). Do not use placeholder paths like `/ruta/al/crate`.

### Doctor (no installs)

```bash
.venv/bin/python -m veyra_proof doctor
```

### List tools

```bash
.venv/bin/python -m veyra_proof list-tools
```

### Plan (no execution)

```bash
.venv/bin/python -m veyra_proof plan tests/fixtures/demo_crate --profile quick
.venv/bin/python -m veyra_proof plan /path/to/rust/workspace --profile standard --dry-run
```

### Audit examples

```bash
# Fixture de demo incluido en este proyecto
.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate \
  --profile standard --install-missing

# O tu propio crate (donde haya un Cargo.toml)
.venv/bin/python -m veyra_proof audit /ruta/real/a/tu/proyecto-rust \
  --profile standard --install-missing

.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate --profile quick
.venv/bin/python -m veyra_proof audit . --profile hardened --yes --install-missing
.venv/bin/python -m veyra_proof audit . --update-lockfile
.venv/bin/python -m veyra_proof audit . --dry-run
```

`--install-missing` asks for confirmation before installing anything; add `--yes` if you
want it to install without asking.

Installs (`cargo install --locked`) compile from source and can take 10–40+ minutes. Without
`--verbose` you only see a progress notice; with `--verbose` cargo's own output streams live:

```bash
.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate \
  --profile standard --install-missing --verbose
```

Works the same on **Windows**, **macOS**, and **Linux** (use your Python launcher / `py -3` on Windows if needed).

## Profiles

Tool ids are the ones `list-tools` and `--include/--exclude` use.

| Profile | Intent |
|---------|--------|
| `quick` | rustfmt, cargo-check, clippy `-D warnings`, cargo-test, audit, veyra-anti-theater, **veyra-e2e** (if scenarios declared) |
| `standard` | quick + nextest, llvm-cov, deny, geiger, machete, dylint (if configured), semver-checks (libs), **veyra-e2e** (if scenarios declared) |
| `hardened` | standard + miri, mutants (configured paths only), fuzz (configured targets), kani (harnesses), vet (existing config), check-external-types, reproducible-build, **veyra-e2e** (if scenarios declared) |

Heavy tools are **SKIPPED** with an explicit reason when configuration/targets are missing.

`reproducible-build` runs two clean builds into throwaway target directories and compares
the final artifacts. It is the most expensive check in `hardened`; use
`--exclude reproducible-build` when that cost is not wanted. Differing hashes are
reported as `INCONCLUSIVE` with both digests, never as a pass.

That finding is informational by default: MSVC/PE output is routinely not
bit-reproducible, so a default that rejects would be flagging a platform rather
than a defect. Both digests stay pinned in the evidence either way. Set
`policy.require_reproducible_build = true` to make bit-identical builds a hard
requirement for your project.

## Where the time goes

Every tool records how long it took, and `report.json` carries a `timing` block plus a
`## Time` table in `report.md`: per-tool seconds, the total, the wall clock and the
difference between them. This matters because the cost is usually the project's own
toolchain, not the audit — auditing an unmodified `rust-lang/regex` takes ~440s of which
~415s is its `cargo test --workspace` suite. Veyra does not cache builds across runs
(shared state that can go stale is how a green audit stops meaning anything) and does not
skip checks to go faster; it makes the bill itemised instead.

## Nightly-only targets

`cargo check` and `cargo clippy` first run with `--all-targets`. A benchmark or example
that uses `#![feature(...)]` cannot build on stable, and one such file used to fail the
whole audit with a wall of unrelated errors. When the *only* failures are nightly feature
gates, Veyra retries without benches and examples and reports **INCONCLUSIVE**, naming
every file it could not verify. That verdict never becomes `OK`, and the log keeps both
attempts. To verify those targets, run on a nightly toolchain.

## Safety defaults

| Action | Default |
|--------|---------|
| Modify `Cargo.toml` | **Forbidden** |
| Rewrite `Cargo.lock` during a run | **Forbidden** (cargo runs with `--locked`) |
| Run `cargo update` | **Forbidden** (needs `--update-lockfile`) |
| Install tools | **Forbidden** (needs `--install-missing`, plus confirmation or `--yes`) |
| `curl \| sh` / arbitrary downloads | **Never** |
| Disable lints/tests to pass | **Never** |

Every cargo invocation that builds the project (`build`, `check`, `test`, `clippy`,
`nextest`, …) is given `--locked` whenever a `Cargo.lock` exists, so a lockfile that has
drifted from `Cargo.toml` surfaces as an error with an instruction instead of being
rewritten by the audit. A stale lockfile is reported **INCONCLUSIVE** — the audit does
not resolve it for you. Run `cargo generate-lockfile` yourself, or pass
`--update-lockfile` to authorize the change.

`--yes` authorizes compatible installs only — **not** lockfile or manifest edits.

`--allow-manifest-changes` records authorization for a future extension; **V1 performs no automatic `Cargo.toml` mutations**.

## Heuristic policy

`veyra-anti-theater` separates what is unambiguous from what is a judgement call.

| Finding | Default | Escalate with |
|---------|---------|---------------|
| `VT001` `todo!()` / `VT002` `unimplemented!()` in a production path | blocker | `fail_on_todo`, `fail_on_unimplemented` |
| `VT003` `panic!()` in `src/` | info | `policy.fail_on_panic = true` |
| `VT004` `#[ignore]` | info | `policy.fail_on_ignored_tests = true` |
| `VT006` test without an observable assertion | info | `policy.fail_on_weak_oracles = true` |
| `VT010` / `VT014` / `VT015` cfg splits, trivial constants | info | `policy.fail_on_cfg_tricks = true` |
| `VT013` broad `#[allow(...)]` | info | `policy.fail_on_lint_allow = true` |
| `VT008` `unsafe` | info | `policy.deny_unsafe = true` |
| non-identical bytes from two clean builds | info | `policy.require_reproducible_build = true` |

There is no honest reading of a shipping function that calls `todo!()`, so those stay
blockers. The rest are ordinary engineering choices that healthy upstream crates make
daily; rejecting an unmodified checkout of a popular crate over them teaches people to
switch the tool off. All of them are always reported — the tool's log opens with a
`rule histogram` and `report.json` keeps every finding at its real severity. Only the
blocking decision changes. Fuzz harnesses (`tests/fuzz/`, `fuzz/fuzz_targets/`) are
excluded from the weak-oracle rule: a libfuzzer target asserts through the engine.

## Tool statuses

`INSTALLED`, `MISSING`, `INSTALLING`, `SKIPPED`, `ERROR`, `OK`, `FINDINGS`, `INCONCLUSIVE`

`OK` is never assigned for timeout, failed install, non-zero exit without analysis, or “not executed.”

## Exit codes (CI)

| Code | Meaning |
|------|---------|
| 0 | Required checks completed without blocking findings |
| 1 | Problems / findings / rejection |
| 2 | Missing prerequisites, unauthorized install, or required tools not executable |
| 3 | Internal application error |

`--fail-on none` accepts the run whatever the findings are; the findings are still in
`report.json`. It is the only value that disables rejection, and it cannot excuse an
unauthorized project file change — that is a broken safety promise, not a tunable finding.

## Evidence output

Default: `PATH/.veyra/evidence/<UTC_TIMESTAMP>/` containing `report.md`, `report.json`, `manifest.json`, per-tool logs, and hashes.

Each tool's logs are pinned by sha256 in the report, so the evidence can be checked against the claim rather than trusted: `python scripts/ci_assert_evidence.py <workspace-path>` runs a real audit of that workspace root and then recomputes the digests, failing when a log was edited after the run, is missing, or backs an `OK` verdict without ever being pinned. The argument is a workspace path (a directory containing `Cargo.toml`), not an evidence directory.

## Optional `veyra.toml`

Not created automatically. See the project specification for the full schema (`critical_paths`, policy flags, fuzz targets, mutants paths, `[e2e]` scenarios, etc.).

## End-to-end behavior (`veyra-e2e`)

Static checks and unit tests cannot tell a wired feature from a placeholder: both compile, and both pass weak tests. `veyra-e2e` builds the workspace with the real toolchain and **drives the binary cargo produced** through the journeys declared in `veyra.toml`, comparing exit code, stdout, stderr and written files against what was declared.

```toml
[e2e]
build_profile = "release"        # the profile you actually ship

[[e2e.scenarios]]
name = "add sums two integers"
argv = ["add", "1", "2"]
expect_exit = 0
expect_stdout_regex = '^3\r?\n?$'

[[e2e.scenarios]]
name = "divide reports a zero divisor"
argv = ["divide", "1", "0"]
expect_failure = true            # any non-zero exit; set expect_exit for an exact one
expect_stderr_regex = 'division by zero'
```

- **No scenarios declared → `SKIPPED`, never `OK`.** No declaration means no verification.
- A scenario that asserts nothing observable (exit 0 and nothing else) is reported as **vacuous** (`E2E00`) instead of passing.
- Every failure quotes what was observed; a timeout is `INCONCLUSIVE`, never a pass and never a failure.
- The binary's sha256, per-scenario exit codes and captured output are written to `tools/veyra-e2e/stdout.log`.
- Runs in `standard` and `hardened` (any profile via `--include veyra-e2e`), and becomes **required** exactly when scenarios are declared.

The demo fixture ships a real CLI (`tests/fixtures/demo_crate/src/main.rs`) wired to its library, with five scenarios covering the happy path, two failure paths, invalid input and `--help`.

## Agent Skill

Canonical skill (model-invoked): `.cursor/skills/veyra-proof/`. It follows the open `SKILL.md` standard, so the same two files work across harnesses.

Four places hold this skill on a normal machine — the canonical directory, the harness fan-out, the Freebuff Desktop account copy and the published copy — and **one of them is the only copy anybody edits**. [SKILL-COPIES.md](SKILL-COPIES.md) says which one you are looking at, who owns the write, what puts it right, and what the link mode costs.

Fan it out to every harness installed on this machine:

```bash
python3 scripts/sync_skill.py                # link every harness target to the canonical skill
python3 scripts/sync_skill.py --check        # report, change nothing (exit 1 if a target needs repair)
python3 scripts/sync_skill.py --copy         # install real copies instead of links
python3 scripts/sync_skill.py --project      # also create project-scoped directories
python3 scripts/sync_skill.py --prune        # install, then report targets left by an older name
python3 scripts/sync_skill.py --prune --apply  # ...and delete them
```

A harness target is a **symlink to `.cursor/skills/veyra-proof`**, not a copy of it. There is therefore no drift to detect and no sync to remember: an edit is live in every harness the moment it is saved, which is what makes "the canonical skill is the only copy anyone edits" a structural fact rather than a habit. `--check` reads the link, so what it reports is whether the harness is pointed at this checkout.

**What the link costs, stated here as well as in the guide.** A link is only as durable as the checkout: move or delete this repository and every harness target goes dead, where a copy would have survived. A filesystem that cannot hold a symlink — Windows without Developer Mode, a home on FAT or some SMB mounts — gets a stamped copy instead, and `sync_skill.py` says so in the `mode` line rather than implying otherwise. The two local loaders in the shipped Freebuff Desktop app read a linked skill directory correctly (both use `statSync`, which follows links), but its Cloud-import path could not be checked from here, so a link in `~/.claude/skills` or `~/.agents/skills` is a change you may want to make with `--copy` held back for those two. Copies are the mode that can drift; they carry the `.veyra-skill-sync` marker to say they were installed on purpose, and the default `--check` accepts a marked copy as deliberate instead of calling it broken.

The `--copy` fallback also keeps the old marker rule: a copy's stamp is what lets `--prune` delete it after a rename, and what lets a machine that runs copies pass the check. Renaming the skill would otherwise leave a live target behind in every harness, still declaring the old name and still triggering — `--check` stays green throughout, because it only inspects the name in use. A harness skills directory is shared with every other skill you have installed, so a directory that is not ours is reported and never replaced, and `--prune` deletes only what it can prove it owns: a directory carrying the stamp, or a link whose target is inside this checkout. Run `--prune` after a rename.

| Harness | Global | Project |
|---|---|---|
| Agent Skills (universal) | `~/.agents/skills` | — |
| Cursor | `~/.cursor/skills` | `.cursor/skills` |
| Claude Code | `~/.claude/skills` | `.claude/skills` |
| Codex | `~/.codex/skills` | — |
| Antigravity | `~/.gemini/config/skills` | `.agents/skills` |
| Freebuff | `~/.config/manicode/.agents/skills` | — |
| OpenCode | `~/.config/opencode/skills` | `.opencode/skills` |
| Cline | `~/.clinerules/skills` | — |
| Goose | `~/.goose/skills` | — |
| GitHub Copilot | — | `.github/skills` |
| Windsurf | — | `.windsurf/skills` |
| Continue | — | `.continue/skills` |
| Trae | — | `.trae/skills` |

A harness counts as installed when its home directory exists; `--all` overrides that. Writes are atomic (temp file plus rename), compared by SHA-256 rather than mtime, and logged to `~/.local/share/veyra-skill-sync.log`.

### The Freebuff Desktop account copy

Freebuff Desktop keeps its own copy of every account skill under `~/.config/freebuff-desktop/account-skills/<account>/<revision>/`, and that copy is deliberately **not** a fan-out target. The app owns it: it re-materializes it from the Cloud snapshot every time it syncs, so a local edit there is overwritten within about a second, and `--check` would report drift the script cannot repair. It is a cache, not a copy.

It goes stale for a different reason. On each sync the app imports your local skills — `~/.claude/skills` and `~/.agents/skills`, in that order, so the second wins — and adds only the skills whose **name is not already in the Cloud snapshot**. `veyra-proof` is already there, so a newer local copy is never re-imported and the account copy keeps the text it was first imported with. That is also why `~/.agents/skills` is a managed target above: without it the app would import the older of its two sources.

After an edit, the account copy catches up only when the stale entry is removed in the app's skill manager, which lets the next sync re-import the corrected local copy. Until then, read it as a cached older release.

Because `sync_skill.py --check` cannot see this store, a second check reports it without touching it. Run both in one pass with one verdict:

```bash
python3 scripts/check_skills.py               # both checks, always both, one verdict
python3 scripts/check_account_skill.py        # just the account copy, change nothing (exit 1 if it lags)
```

`check_skills.py` runs the fan-out check and the account check every time and prints both reports plus a verdict, so neither can be forgotten. It applies one policy to the two: **harness drift fails**, because this repository can repair it, and **the account copy warns without failing**, because it cannot — a contributor whose Cloud cache is stale would otherwise never see a green check, and a check that can never go green is one people learn to ignore. A sub-check that cannot run at all (exit 2) fails the command, since that is a defect in the tooling rather than an out-of-date copy.

It compares the canonical skill against both faces of the store — the cached Cloud snapshot (`current.json`) and each materialized `veyra-proof` directory, which is the file an agent reads today — and prints `current`, `OLDER` or `DIFFERS` per copy with the version and byte counts behind the verdict. Exit 1 means the account copy lags the repo; the output names the one action that fixes it. A machine with no account store exits 0: absence is not drift. CI runs the same script as an advisory job, so drift is visible in a workflow run without failing it.

`Veyra/` is a standalone, publishable copy of this skill for public distribution. It carries its own `install.py` (standard library only, no `--all` needed) and is kept byte-identical to the canonical copy by `tests/test_skill_document.py`.

The safety rules in `SKILL.md` sit inside a `HARD_RULES` region that no automated mutation may edit. `tests/test_skill_document.py` enforces that, plus harness neutrality (no machine-local paths) and the token budget. Run it with the rest of the suite:

```bash
.venv/bin/python -m pytest
```

Triggers include: install veyra, auditar Rust, audit a Cargo workspace, wire veyra. Optional env: `VEYRA_PROOF_HOME`, `VEYRA_PROOF_BIN`.

### Maintaining the skill

`SKILL.md` is the only file loaded into the agent's context when the skill triggers, so keep it lean. Keep maintainer-facing material (portability tables, sync commands, editing policy) here in the README, not in the skill.

- Never edit inside `HARD_RULES_START` / `HARD_RULES_END`.
- Prefer one bounded add, delete, or replace over a wholesale rewrite.
- Keep `SKILL.md` under ~1,600 tokens; move lookup-only material to `reference.md`.
- Keep the frontmatter `metadata.version` equal to `pyproject.toml`'s version, and bump all three together (`pyproject.toml`, `src/veyra_proof/__init__.py`, the skill). `tests/test_docs_match_code.py` fails if the skill names a release that is not the one shipping.
- Stay harness-neutral: no absolute machine paths, no vendor CLI names, no editor-only commands.
- If you adopt [SkillOpt](https://github.com/microsoft/SkillOpt), move the hard rules into its `SLOW_UPDATE` region, which normal edit patches cannot modify.
- After any edit: `python3 -m pytest` then `python3 scripts/check_skills.py`. `sync_skill.py` is still worth running after a checkpoint is created or moved, or after a harness is installed: it installs the link, and re-points one that has gone stale. There is nothing to repair after an edit — with links in place the harnesses already read what you just changed.
- After renaming the skill or the folder: `python3 scripts/sync_skill.py` then `python3 scripts/sync_skill.py --prune --apply`.
- Before tagging a release: `python3 scripts/pre_release.py --tag v0.4.3 --title "..."`. That is the whole checklist in one command; see [The pre-release checklist](#the-pre-release-checklist) for the ways it refuses.

## Packaging with PyInstaller

Build **on each target OS/arch** (no universal cross-compile promise):

```bash
python -m pip install pyinstaller
pyinstaller --onefile --name veyra -m veyra_proof
# or:
pyinstaller --onefile --name veyra src/veyra_proof/__main__.py
```

| Target | Build host |
|--------|------------|
| Windows x86_64 | Windows x86_64 |
| macOS Apple Silicon | macOS arm64 |
| macOS x86_64 | macOS x86_64 (or CI runner) |
| Linux x86_64 | Linux x86_64 |

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it proves |
|---|---|
| `test` | The full test suite on ubuntu, macos and windows across Python 3.10 and 3.13, plus `veyra --version` from the installed console script |
| `audit` | A real audit of the demo crate on a real Rust toolchain, followed by a check that the evidence on disk backs the verdict |
| `account-skill` | Whether the Freebuff Desktop account copy of the skill still matches the canonical one — advisory, see below |

The `test` suite mocks `subprocess.run`, so on its own it cannot catch a broken tool invocation. The `audit` job closes that gap: it runs `scripts/ci_assert_evidence.py`, which executes the auditor for real and then requires that every tool reported `OK` has a log on disk *and* a per-tool `result.json` agreeing with the summary on both status and exit code. Two independently written artifacts must tell the same story.

That check has its own tests (`tests/test_ci_assert_evidence.py`), which inject each fault it is meant to catch. A verification script that cannot fail is worse than none: it manufactures the false confidence this project exists to refuse.

The `account-skill` job is advisory on purpose, and it is worth being exact about what it does and does not do. GitHub's own runners have no Freebuff Desktop store, so there it prints `no account store` and passes; its value is on a self-hosted runner or a developer machine that has one, where a lagging copy surfaces as a warning annotation and in the run summary rather than as a red build. A stale Cloud cache must not fail somebody's pull request, because that cache is machine-local and this repository cannot repair it. The step still fails the job when `scripts/check_account_skill.py` exits 2 or worse — a checker that cannot run is a defect here, not a condition of someone's machine.

## Publishing to PyPI

`.github/workflows/publish.yml` builds the sdist and wheel and uploads them on a published release, using [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC). **No API token is stored in the repository and there is nothing to rotate.**

One-time setup on PyPI, for the project `veyra-proof`:

1. Create the project at <https://pypi.org/manage/account/publishing/> and add a pending publisher: owner `Doken-Tokuyama`, repository `Veyra-Proof`, workflow `publish.yml`, environment `pypi`. These have to match the OIDC claims GitHub sends, which name the account that owns the repository — a stale owner publishes nothing and fails with `invalid-publisher`.
2. Create a `pypi` environment on the repository. The publish job declares it, which is what gates a release to someone who can approve it.

The sdist carries the agent skill, the fan-out script, the CI workflows and the test suite, not just the importable package — half of what this project ships is the skill. `.cursor` is listed file by file in `MANIFEST.in` rather than by wildcard, so a stray editor or agent state directory in a working copy can never ride along.

## The pre-release checklist

One command runs the whole checklist, writes the release notes and cuts the tag:

```bash
python -m pip install --upgrade build twine   # the build step needs both
python scripts/pre_release.py --tag v0.4.3 --title "one line the release can be read by"
```

Eight steps, all reported whether or not an earlier one failed, so one pass tells you everything that is wrong:

| Step | What it proves |
|---|---|
| `tests` | The suite, which carries the fan-out, version and docs guards |
| `platforms` | That same suite, on the commit being tagged, as CI actually ran it on Linux, macOS and Windows |
| `copies` | Every harness target is the canonical skill |
| `build` | An sdist and a wheel, then `twine check` on **this version's** artefacts only |
| `wheel` | That wheel, installed into a throwaway venv, reporting the version it was built as |
| `evidence` | A real audit whose on-disk evidence matches its own verdict |
| `published` | What the package index serves for this version, compared with the artefact those steps checked |
| `notes` | The release notes, generated from `changes.md` and the numbers these steps measured |

The `wheel` step exists because source files agreeing with each other is not the claim a release makes. 0.4.1 shipped a wheel whose `__version__` still read `0.3.0` while the distribution said 0.4.1: `veyra --version` answered `veyra 0.3.0`, and every `manifest.json` that wheel wrote signed its evidence with the wrong version. `scripts/check_wheel_version.py` installs the wheel into a temporary virtual environment and reads the version back from four independent places — the installed metadata, `veyra_proof.__version__`, the `veyra` console script the wheel declared, and `python -m veyra_proof` — with `PYTHONPATH` scrubbed and the import required to resolve inside the venv, so an answer taken from this checkout's `src/` cannot pass for an answer from the artefact. It is a real `pip install`, so a wheel with dependencies has them resolved (`--no-deps` exists for an offline machine); the environment is removed on success and left in place, with its path printed, when something disagrees.

The `published` step exists because every other step looks at **this checkout**, and nobody installs this checkout. `publish.yml` builds its own sdist and wheel from the tagged commit and uploads those, so what the index serves is a second build that no local step ever opened: v0.5.0 shipped with that comparison made by hand, once, in a terminal, and it could have come out differently without the gate noticing. `scripts/check_published.py` asks the index for the version being released and compares the two artefacts **member by member** — the set of member names and the sha256 of each member's content — because wheels and sdists are not byte-reproducible by default (a zip stores each member's timestamp, gzip one of its own), so two builds of one commit give different bytes and identical payloads; byte-identity is reported only when it actually holds. It has three answers and only one of them is a verdict: **no files for this version** passes, because publishing happens *after* the gate, and the facts record `published: false` so the notes say "nothing yet" rather than carrying a row that reads like a verification; **every payload matches** passes and the row names how many members were compared; a **differing payload**, a served file this checkout cannot show, or a digest that does not describe the bytes served fails the gate, naming what differs. A machine that cannot reach the index exits 2 and the tag is refused, the same policy `platforms` applies to CI. Because the answer moves while the tree does not, this step is **never resumed**: it is re-asked on every run, which is how a run on an unchanged tree after the release is published fills that row in — the other steps resume in a fraction of a second, so filling it costs one index request and one notes regeneration. The comparison is between the index and the artefacts **this run** built, which is what makes the two readings distinguishable: on the tree a release was cut from, a difference is a defect; on a tree that has moved past the release, the artefacts it builds are simply not the published ones, and the step says so — naming the tag and the checkout — rather than inviting somebody to repair a build that is fine. `--index-url` takes either spelling — `https://pypi.org` or pip's `https://pypi.org/simple` — so TestPyPI works.

The `platforms` step exists because one machine cannot answer the question a release asks. v0.4.3 was tagged and published from this checkout while both of its Windows jobs were already failing: CI reported it only afterwards, so the fix had to ship as 0.4.4 rather than 0.4.3 being green. `scripts/check_platforms.py` reads the CI run for **the exact commit being tagged** and requires every job of it to have concluded `success` and every platform named in `REQUIRED_PLATFORMS` to have a successful job of its own — cancelled and skipped are not passes either. A commit with no run is refused with the instruction that fixes it (push it, let CI finish, run the gate again), and if this machine cannot ask GitHub at all — no `gh`, no credentials, no network — the step exits 2 and the gate refuses the tag: an unverifiable release is left unproven rather than assumed green. Nothing is mutated to make it pass. `tests/test_check_platforms.py` holds `REQUIRED_PLATFORMS` against the matrix in `.github/workflows/ci.yml`, so dropping a platform from CI fails the suite instead of quietly shrinking what a release claims.

**No number in a release is typed by hand.** The first version of the v0.4.4 notes carried a table reading `351 passed`, an evidence directory named by its timestamp and a count of tools that came back `OK` — each of them copied out of a terminal, and each of them right only for as long as nobody rebuilt anything; the v0.4.1 draft is the other shape of the same problem, opening with a note that its headline counts no longer matched the suite. So the steps that can measure something now state it as JSON (`--report`) instead of only printing it: pytest's counts come from the JUnit XML it writes, and `check_platforms.py`, `check_wheel_version.py`, `ci_assert_evidence.py` and `check_published.py` each write the facts they observed — including the auditor version that produced the evidence, and how many members of the published artefact matched. The gate records those facts in `.git/veyra-pre-release.json` beside the resume entries, and the `notes` step turns them into `.freebuff/release-notes-<version>.md`, the file `gh release create --notes-file` needs. A step whose measurement is missing gets **no row** in the table plus a `## Not measured` line naming it: an absent row is a visible limitation, a plausible number would be a lie. `scripts/release_notes.py` refuses outright when the record does not cover the tree being released — the fingerprint is the gate's own function, looked up rather than reimplemented — so notes cannot be regenerated from measurements taken on a different commit. `notes` is deferred to run last, since it reads what the other steps measured; it is still a step, so a run that cannot generate them does not tag.

`--title TEXT` is the release headline: it becomes the tag annotation (`NAME — TEXT`) and the title of the generated notes. It no longer requires `--tag`, because the notes are written on every passing run — so a release can be prepared, measured and read before anybody cuts the tag.

`--tag NAME` creates the tag only after every step has passed, as an **annotated** tag reading `NAME — TITLE` (`--title`), which is the form every existing tag in this repository uses and the same string GitHub shows as the release name; `git tag NAME` alone would make a lightweight tag, the one tag in the history that `git describe` reduces to a bare hash. It refuses several ways round: when any step failed; when `--skip` was used, because a tag gated by a partial checklist is not gated; when `NAME` is not `v` plus the version `pyproject.toml` declares; when the tag already exists; and when the tracked skill files are uncommitted — that last one matters because `publish.yml` builds the sdist from the tagged commit, so a working tree whose copies all agree locally still says nothing about what ships. `git status` alone cannot see that: it hides ignored paths, so a skill file matched by an ignore rule reads as clean while git would never commit it, so tracking is checked explicitly.

**It resumes, so an interruption does not cost you the whole run.** Every step that passes is recorded against a fingerprint of the tree — `HEAD` plus the contents of the tracked and untracked-but-unignored files — in `.git/veyra-pre-release.json`, along with how long it took and **what it measured**, so a resumed run keeps the measurement instead of re-taking it and the notes describe a real run rather than a partial one. A run that dies after the tag starts the next one with `PASS (resumed)` for what this exact tree already verified and goes straight on: measured on this checkout, the full checklist took 22.4 s, 29.4 s and 35.4 s on three runs and the resumed runs took 0.14 s, 0.15 s and 0.13 s. Any change to the tree invalidates every record, a record made for another version is ignored, an unreadable ledger is treated as empty, and a failure is never recorded at all — so resuming can only ever skip a success, never inherit one. A tag this gate created on an earlier attempt is recognised as its own instead of refused; a tag that differs from it — lightweight, a different annotation, another commit — is still refused, with the difference named. Delete the ledger file to force the full run. It fingerprints the tree, not the toolchain: switching `rustc`, `clippy` or `cargo` does not invalidate a record, so a resumed run inherits the toolchain the recorded pass was made with — delete the ledger after a toolchain upgrade. The ledger path comes from `git rev-parse --absolute-git-dir` rather than being assumed to be `.git/`, so the gate also works from a linked worktree, where `.git` is a file; if the ledger cannot be written the run says so and continues, because a cache that can end a release is worse than the resume it saves.

A step whose answer lives outside the checkout is not resumed, and there are two of them: the notes (a file somebody can delete) and the index comparison (the index changes while the tree does not). Everything else is, and the record keeps the measurement it took.

When a tag exists, the run ends by saying what is left and what it will not do: whether this commit is on `origin/main`, the `git push` for the tag, and the `gh release create` for the release body taken from `.freebuff/release-notes-<version>.md` — the file the `notes` step just generated. That directory is ignored by git, deliberately: the notes are built from the record of one run, and committing them would give a later run a stale body to publish by accident. Publishing the release is what triggers `publish.yml` and uploads to PyPI, so the gate stops at the tag — that part cannot be undone, and it is not something a checklist should do behind your back.

**The limit, stated plainly: git has no pre-tag hook.** `python scripts/pre_release.py` (no `--tag`) runs the same checklist and reports; anyone who runs `git tag` directly bypasses all of it. This gate is real only for people who tag through it.

A stale Cloud account copy does not block a release: that cache is machine-local and is not what a release ships. The `copies` step reports it as a warning and passes.

## Tests

```bash
cd Veyra-Proof
.venv/bin/python -m pytest
```

Tests mock `subprocess.run` and do not install Rust tools or mutate real projects.

## Contact

[contact@exyonq.org](mailto:contact@exyonq.org)

## Security

Report security problems to [security@exyonq.org](mailto:security@exyonq.org). See [SECURITY.md](SECURITY.md).

## License

MIT — Copyright (c) 2026 Antonio Cantallops Alba. See `LICENSE`.
