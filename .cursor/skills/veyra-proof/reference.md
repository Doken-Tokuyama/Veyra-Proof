# Veyra reference

## Profiles

| Profile | Intent |
|---------|--------|
| `quick` | rustfmt, cargo-check, clippy `-D warnings`, cargo-test, audit, veyra-anti-theater, veyra-e2e (if scenarios declared) |
| `standard` | quick + nextest, llvm-cov, deny, geiger, machete, dylint (if configured), semver-checks (libs) |
| `hardened` | standard + miri, mutants (configured paths), fuzz (configured targets), kani, vet, check-external-types, reproducible-build |

Heavy tools SKIP with an explicit reason when config/targets are missing.

`reproducible-build` runs two clean builds and compares the final artifacts; it is
the most expensive check in `hardened` and `--exclude reproducible-build` turns it
off. Differing hashes are `INCONCLUSIVE` with both digests, never a pass, and are
informational unless `policy.require_reproducible_build = true`.

Every tool records its own duration: `report.json` has a `timing` block and
`report.md` a `## Time` table with per-tool seconds, the total and the wall clock.
Quote it when someone asks why an audit took minutes — on a real crate the cost is
usually its own test suite. Veyra neither caches builds across runs nor skips checks
to go faster.

## Verdicts that are not passes

| Situation | Verdict |
|-----------|---------|
| `Cargo.lock` out of date with `Cargo.toml` | `INCONCLUSIVE` — cargo runs with `--locked`, and Veyra will not rewrite the lockfile |
| Manifest declares dependencies, no lockfile | `SKIPPED` + `unlocked-dependencies` — dependency safety is unknown |
| A target needs nightly (`#![feature]`) | `INCONCLUSIVE`, naming the files not verified; lib/bins/tests are still checked |
| Scenario timed out, tool timed out | `INCONCLUSIVE` |

## Heuristic policy (`veyra.toml` `[policy]`)

`todo!()` / `unimplemented!()` in a production path are blockers. Everything else is
reported and only blocks when asked for: `fail_on_ignored_tests`, `fail_on_panic`,
`fail_on_lint_allow`, `fail_on_weak_oracles`, `fail_on_cfg_tricks`, `deny_unsafe`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Required checks completed without blocking findings |
| 1 | Problems / findings / rejection |
| 2 | Missing prerequisites, unauthorized install, or required tools not executable |
| 3 | Internal application error |

## End-to-end behavior (`veyra-e2e`)

Builds the workspace with the real toolchain and runs the binary cargo produced through the journeys declared in `veyra.toml`, comparing exit code, stdout, stderr and written files with what was declared. It only proves what the scenarios cover.

```toml
[e2e]
build_profile = "release"     # or "debug"
features = []                 # feature flags of the shipped configuration
no_default_features = false
binaries = []                 # defaults to the workspace's first built bin target
timeout_seconds = 60

[[e2e.scenarios]]
name = "add sums two integers"
argv = ["add", "1", "2"]
expect_exit = 0
expect_stdout_regex = '^3\r?\n?$'
# stdin = ""
# expect_stderr_regex = 'division by zero'
# expect_files = ["out/report.txt"]
# expect_failure = true       # any non-zero exit; set expect_exit for an exact one
```

| Outcome | Meaning |
|---------|---------|
| `OK` | every scenario observed what it declared |
| `FINDINGS` | `E2E00` vacuous scenario, `E2E01` exit mismatch, `E2E02` output/file mismatch, `E2ECFG` invalid configuration, `E2E03` informational reachability note |
| `INCONCLUSIVE` | the build or a scenario timed out, or the workspace builds no binary |
| `SKIPPED` | no scenarios declared: behavior was **not** verified |
| `ERROR` | `cargo build` failed; nothing was executed |

Required in every profile exactly when scenarios are declared (and available to `--include`). `E2E03` never blocks a verdict.

## Evidence

Default: `PATH/.veyra/evidence/<UTC_TIMESTAMP>/` with `report.md`, `report.json`, `manifest.json`, per-tool logs, hashes.

Every log a tool writes — `stdout.log`, `stderr.log`, and `skip.txt` when a tool skips after running — is pinned by its sha256 in that tool's `artifact_hashes`, so the report states what its own evidence held. `scripts/ci_assert_evidence.py` recomputes those digests and fails a report whose log changed after the run, was deleted, or is not pinned at all for an `OK` verdict.

## Safety defaults

| Action | Default |
|--------|---------|
| Modify `Cargo.toml` | Forbidden |
| Rewrite `Cargo.lock` during a run | Forbidden — cargo runs with `--locked` |
| `cargo update` | Needs `--update-lockfile` |
| Install tools | Needs `--install-missing` + confirm or `--yes` |
| `curl \| sh` | Never |

`--fail-on none` accepts the run whatever it found; the findings stay in `report.json`.
It is the only value that disables rejection, and it cannot excuse an unauthorized
project file change.

## Packaging note

PyInstaller binaries are OS/arch-specific. Build on the target host; unsigned macOS binaries may need Gatekeeper “Open anyway.”
