# Veyra Proof

[![CI](https://github.com/ExyonQ/Veyra-Proof/actions/workflows/ci.yml/badge.svg)](https://github.com/ExyonQ/Veyra-Proof/actions/workflows/ci.yml)
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
git clone https://github.com/ExyonQ/Veyra-Proof
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

`--install-missing` pedirá confirmación antes de instalar; añade `--yes` si quieres que lo haga sin preguntar.

Las instalaciones (`cargo install --locked`) compilan desde fuente y pueden tardar 10–40+ minutos. Sin `--verbose` verás solo un aviso de progreso; con `--verbose` el output de Cargo se muestra en vivo:

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

Fan it out to every harness installed on this machine:

```bash
python3 scripts/sync_skill.py                # write missing or stale copies
python3 scripts/sync_skill.py --check        # report drift, change nothing (exit 1 if stale)
python3 scripts/sync_skill.py --project      # also create project-scoped directories
python3 scripts/sync_skill.py --prune        # report copies left by an older name (dry run)
python3 scripts/sync_skill.py --prune --apply  # ...and delete them
```

Renaming the skill would otherwise leave a live copy behind in every harness, still declaring the old name and still triggering — `--check` stays green throughout, because it only inspects the name in use. Every copy the script writes is stamped with a `.veyra-skill-sync` marker whose filename never tracks the skill name, so the stamp survives the rename that made the copy stale; `--prune` deletes only directories carrying it. A harness skills directory is shared with every other skill you have installed, so anything without the stamp is left alone. Run `--prune` after a rename.

| Harness | Global | Project |
|---|---|---|
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
- Stay harness-neutral: no absolute machine paths, no vendor CLI names, no editor-only commands.
- If you adopt [SkillOpt](https://github.com/microsoft/SkillOpt), move the hard rules into its `SLOW_UPDATE` region, which normal edit patches cannot modify.
- After any edit: `python3 -m pytest` then `python3 scripts/sync_skill.py`.
- After renaming the skill or the folder: `python3 scripts/sync_skill.py` then `python3 scripts/sync_skill.py --prune --apply`.
- Before tagging a release: `python3 -m build && twine check dist/*`, and `python3 scripts/ci_assert_evidence.py tests/fixtures/demo_crate` to confirm the auditor still produces evidence that matches its own verdict.

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

The `test` suite mocks `subprocess.run`, so on its own it cannot catch a broken tool invocation. The `audit` job closes that gap: it runs `scripts/ci_assert_evidence.py`, which executes the auditor for real and then requires that every tool reported `OK` has a log on disk *and* a per-tool `result.json` agreeing with the summary on both status and exit code. Two independently written artifacts must tell the same story.

That check has its own tests (`tests/test_ci_assert_evidence.py`), which inject each fault it is meant to catch. A verification script that cannot fail is worse than none: it manufactures the false confidence this project exists to refuse.

## Publishing to PyPI

`.github/workflows/publish.yml` builds the sdist and wheel and uploads them on a published release, using [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC). **No API token is stored in the repository and there is nothing to rotate.**

One-time setup on PyPI, for the project `veyra-proof`:

1. Create the project at <https://pypi.org/manage/account/publishing/> and add a pending publisher: owner `ExyonQ`, repository `Veyra-Proof`, workflow `publish.yml`.
2. Create a `pypi` environment on the repository. The publish job declares it, which is what gates a release to someone who can approve it.

The sdist carries the agent skill, the fan-out script, the CI workflows and the test suite, not just the importable package — half of what this project ships is the skill. `.cursor` is listed file by file in `MANIFEST.in` rather than by wildcard, so a stray editor or agent state directory in a working copy can never ride along.

Check a build locally before tagging:

```bash
python -m pip install --upgrade build twine
python -m build
twine check dist/*
```

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
