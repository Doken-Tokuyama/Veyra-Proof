## What changed in v0.4.0

A skill-document and release-metadata release. The `veyra` CLI, exit codes, and
`report.json`/`report.md` schema are unchanged from 0.3.0; what improved is the agent skill
that drives the tool and the declared version. Validated by the test suite (skill-document
guards and the byte-identical fan-out), not by new tool runs.

### Skill improvements

- **Result status vocabulary documented** (`reference.md`, fanned out to `Veyra/skills/`):
  the exact `ResultStatus` set — `OK`, `FINDINGS`, `ERROR`, `SKIPPED`, `INCONCLUSIVE` — with
  strict meanings, `MISSING` as the detection status for an absent tool, and the `ACCEPTED` /
  `REJECTED` / `INCONCLUSIVE` decision. A status is a claim about what was observed; `OK` is
  never assigned to a tool that did not run.
- **Coverage honesty documented** (`reference.md`): `cargo check` omits code generation, a
  workspace's default member selection can be narrower than `--workspace`, and
  `cargo test --no-fail-fast` is needed to gather every test executable — do not claim more
  coverage than a command provided.
- **Frontmatter** (`SKILL.md`): added `license: MIT` and `metadata.version: 0.4.0`, the
  portable version location for the open `SKILL.md` standard. The frozen `HARD_RULES` block
  is unchanged.

`SKILL.md` stays within its 1,600-token budget, so the new detail lives in `reference.md`
(loaded on demand) rather than the always-loaded body.

### Migration

None required. Commands, exit codes, the report schema, and `veyra.toml` are identical to
0.3.0. The visible changes are the skill document and the declared version. `Veyra/skills/`
stays byte-identical to `.cursor/skills/`, enforced by `tests/test_skill_document.py`.

---

## What changed in v0.3.0


Everything below was verified by running the tool, not only by unit tests. The test
suite went from **130 → 251 tests**, all green, and every defect has a regression test
that fails before the fix.

---

## How this was found

An adversarial pass: the released tool was installed from the tag, the documented quick
verification was run, then ten deliberately broken crates were audited — plus an
unmodified checkout of `rust-lang/regex` to see how it behaves outside its own fixture.
Every claim below is a verdict that was actually observed, with the evidence directory
that backs it.

Detection that already worked and **still** works (re-verified after every fix):
`todo!()`/`unimplemented!()` blockers, a CLI wired to the wrong logic (`E2E01`/`E2E02`),
RUSTSEC advisories, formatting, clippy, failing tests, compile errors, vacuous scenarios,
timeouts as `INCONCLUSIVE`, and six separate forgeries of the evidence being rejected.

---

## Defects fixed

### 1. The audit rewrote your `Cargo.lock`

No cargo invocation used `--locked`, so `cargo check` resolved and rewrote a stale
lockfile mid-audit. The tool then reported its own mutation as an unauthorized change and
rejected the run.

* `apply_lock_policy()` (`command_runner.py`) adds `--locked` to every cargo build
  (`build`, `check`, `test`, `clippy`, `nextest`, …) when a lockfile exists, and never
  adds it when one does not — `--locked` on a project with no lock makes cargo refuse to
  build at all.
* `Tool.cargo_command()` routes every tool through it, so **plan = ejecución = evidencia**.
  Before, the report recorded a command the tool had not actually run.
* A stale lockfile is now `INCONCLUSIVE` with an instruction, not a bare
  "Non-zero exit code 101".
* `--update-lockfile` still authorizes the change, and `plan` states the policy.

Verified: lockfile byte-identical after a drifted run, verdict `INCONCLUSIVE`; with
`--update-lockfile` the lock is updated and marked `[authorized]`.

### 2. `cargo check --all-targets` failed on stable for most real projects

A benchmark using `#![feature(test)]` cannot build on stable, and that failed the whole
audit with a wall of unrelated errors. On `rust-lang/regex` this alone made the run
unusable.

* When the only failures are nightly feature gates, `cargo check` and `cargo clippy`
  retry without benches/examples and report **`INCONCLUSIVE` naming every unverified
  file**. Never `OK`. Both attempts stay in the log.
* A failure on the stable targets too stays `ERROR`.
* If any *other* error code is present, no retry happens.

Verified on regex: `cargo-check` `INCONCLUSIVE` ("1 target file(s) need a nightly
toolchain and were NOT verified") instead of an opaque `ERROR`.

### 3. `veyra-e2e` reported `duration_seconds: 0.0`

`_finish()` never passed the field, so the tool that compiles the workspace and drives the
binary reported zero time. Measured 7.5s of real work reported as 0.0. Anti-theater had
the same hardcoded `0.0`.

Both now measure wall clock. Verified: e2e reports its real duration.

### 4. `reproducible-build` was documented in `hardened` but belonged to no profile

* Added to `PROFILE_ORDER["hardened"]`.
* The root cause was worse than a missing entry: `PROFILE_ORDER` (which selects tools)
  and `default_profiles` (which `list-tools` publishes) were **two independent lists**,
  and they had already diverged. `profile_mismatches()` now reports any disagreement and
  a test fails if it is non-empty.

Verified: `hardened` runs it, reports differing artifacts, and pins both digests.

### 5. `cargo-audit` never ran on the first audit of a project

The lockfile check happened at discovery time. A brand-new project has no lock, so the
very first audit — the one that matters — reported nothing about dependencies.

* The check now re-reads the file at run time; cargo has already written the lock.
* With dependencies declared and no lock, the tool is `SKIPPED` **plus** an
  `unlocked-dependencies` finding saying dependency safety is unknown.

Verified: a fresh crate depending on `time 0.1.44` is now rejected with
`RUSTSEC-2020-0071` on its first run.

### 6. 277 false positives on an unmodified upstream crate

Auditing `rust-lang/regex` produced 277 findings and a `REJECTED` verdict: 114 "test has
no observable assertion" inside fuzz regression harnesses, 32 `panic!()` documenting
invariants, 10 `#[allow]`, 5 `#[ignore]`, and cfg heuristics. A tool that rejects
`rust-lang/regex` out of the box gets switched off.

The split applied:

| Rule | Default | Escalate with |
|------|---------|---------------|
| `VT001`/`VT002` `todo!()`/`unimplemented!()` in production | **blocker** | `fail_on_todo`, `fail_on_unimplemented` |
| `VT003` `panic!()` | info | `fail_on_panic` |
| `VT004` `#[ignore]` | info | `fail_on_ignored_tests` |
| `VT006` test without an oracle | info | `fail_on_weak_oracles` |
| `VT010`/`VT014`/`VT015` cfg, trivial constants | info | `fail_on_cfg_tricks` |
| `VT013` broad `#[allow]` | info | `fail_on_lint_allow` |
| `VT008` `unsafe` | info | `deny_unsafe` |

There is no honest reading of a shipping function that calls `todo!()`, so those stay
blockers. Everything else is reported at its real severity and only blocks when the
project asks. Fuzz harnesses (`tests/fuzz/`, `fuzz/fuzz_targets/`) and `#[ignore]`d tests
are excluded from the weak-oracle rule. The tool's log opens with a `rule histogram` and
`report.json` keeps every finding.

Verified on regex: 255 informational findings, **0 blocking**, verdict no longer driven
by heuristics.

### 7. `--fail-on none` did not disable anything

Error-severity findings rejected before the flag was consulted. It is now the only value
that disables rejection — and it still cannot excuse an unauthorized project file change,
which is a broken safety promise rather than a tunable finding.

Related: a tool whose overall status is `INCONCLUSIVE` can still have *observed* something
concrete (`reproducible-build` comparing digests). Those findings now count for
`--fail-on findings` instead of slipping through on the status field, and the rejection
reason names the tool and rule.

### 8. The README contradicted the code

* `hardened` claimed `reproducible-build` (did not run).
* "The 76-test suite" had drifted.
* `ci_assert_evidence.py <path>` suggested an argument its argparse rejects.
* Safety defaults omitted the lockfile.

Corrected, and `tests/test_docs_match_code.py` now fails the build if the README and the
registry disagree about any profile, if it pins a test count, or if it omits a documented
safety rule. Documentation that contradicts the code is the same defect as a status that
contradicts its log.

### 9. Found while fixing #1 — undecodable output produced empty evidence

Not introduced by these changes; present in the released version.

On Windows, `subprocess.run(text=True)` decodes with the locale codec. A single byte
outside cp1252 killed the reader thread, leaving `stdout`/`stderr` as `None`: the tool
wrote **two empty logs** and still reported
`Non-zero exit code 101 without structured findings`. On regex this hid 873 real clippy
findings behind an empty evidence file.

* Both the capturing and streaming paths decode UTF-8 with `errors="replace"`.
* `ci_assert_evidence.py` now rejects a `FINDINGS`/`ERROR` result whose every captured
  log is empty. A silent `cargo fmt --check` stays legal — that emptiness is a real pass.

Verified: clippy on regex now reports its 873 findings with logs to match.

### Also fixed

* `todo!()` inside `#[cfg(test)]` was counted as a production placeholder (surfaced by a
  new test; `VT003` already excluded test modules, `VT001`/`VT002` did not).
* **A doc comment naming a macro was read as a call to it.** Building the blind-spot
  fixture below, `VT001`/`VT002` fired on a module doc that *explained* the crate
  contains no `todo!()` — a blocker raised from prose. Every macro/attribute rule now
  reads comment-free code (string-literal aware, inline and block comments).
* An `INCONCLUSIVE` result whose findings are all informational now says so in its
  summary instead of reading like a problem.
* The reason for a rejection names the tools and rules that caused it.

---

## The blind spot, measured

To answer "does it work" honestly, I built the strongest case against its headline
claim: a discount engine that is **fake and invisible** — real types, a doc comment
citing business rule PRD-114, a plausible arithmetic body, tests that assert whatever the
code currently does, formatted, and clippy-clean. `quick`:

```
- Decision: ACCEPTED          exit 0
  OK  rustfmt · cargo-check · clippy · cargo-test · cargo-audit · veyra-anti-theater
  SKIPPED  veyra-e2e — no [[e2e.scenarios]] declared … (this is not a pass)
```

Then the identical code plus a `veyra.toml` declaring the three real journeys
(bronze → 0, silver → 500, gold → 1000 on a 10 000 basket):

```
- Decision: REJECTED          E2E02 bronze customers earn nothing back (PRD-114)
                              E2E02 silver customers earn 5% back (PRD-114)
```

So: **the heuristics cannot see a well-written fake, and nothing regex-shaped can.** The
`veyra-e2e` layer catches it — but only for journeys somebody declared, which is the
manual, high-value part of the wiring. What matters is that the first run did not *claim*
green-with-confidence: it reported `ACCEPTED` and, one line below, said end-to-end
behaviour was not verified and that this is not a pass. That is the design holding.

This is now stated in the README's honest limitations, with a test that fails if the
sentence disappears.

---

## Decisions taken on the two open questions

### The ~430s `cargo test` on regex: measure, don't hide

Measured before deciding: **444s warm, 529s cold**. Only 85s of that is compilation —
the other ~415s is regex's own `cargo test --workspace` suite, exiting 0. There is no
defect to fix in Veyra here.

Caching builds across runs was rejected: shared state that can go stale is exactly how a
green audit stops meaning anything. Skipping or overlapping checks to look faster was
rejected: a check that did not run cannot be reported as one that passed. Cargo tools also
serialize on the target directory lock, so parallelizing them would contend, not help.

Instead the cost is now itemised in the evidence — `timing` in `report.json` and
`manifest.json`, and a `## Time` table in `report.md`:

```
-     10.2s  miri (OK)
-      3.7s  llvm-cov (OK)
-      3.5s  reproducible-build (INCONCLUSIVE)
- 7 tool(s) did not run: dylint, semver-checks, mutants, fuzz, kani, vet, check-external-types
- total in tools: 28.92s of 30.37s wall
```

A tool that ran but recorded no duration is listed separately from one that never ran,
so the line that would indicate a regression cannot be mistaken for the normal case.

### `hardened` doing two full builds: keep the check, make blocking opt-in

The check now runs in `hardened`, so the README's promise is kept. But on MSVC/PE output
the two clean builds almost always differ, which under the old defaults rejected every
Windows project for a platform property rather than a defect — the same false-positive
class as #6, arrived at through a different door.

So the check follows the same rule as every other judgement call: it runs, it reports
`INCONCLUSIVE` with both digests pinned as evidence, and its finding is informational
unless the project declares reproducibility a requirement via
`policy.require_reproducible_build = true`, which makes it an error and rejects the run.

`--exclude reproducible-build` remains the cost escape hatch. Verified: `hardened` on the
demo fixture is `ACCEPTED` with the mismatch visible and both digests recorded.

---

## Files touched

```
README.md                                        profiles, lockfile rule, heuristic policy,
                                                 nightly behaviour, time accounting
scripts/ci_assert_evidence.py                   rejects FINDINGS/ERROR with empty logs
src/veyra_proof/command_runner.py                lock policy, UTF-8 decoding (+121)
src/veyra_proof/reporting.py                     decide() ordering, timing accounting (+38)
src/veyra_proof/tool_registry.py                 PROFILE_ORDER + profile_mismatches() (+39)
src/veyra_proof/config.py                        5 new policy knobs, 2 defaults changed
src/veyra_proof/cli.py                           lock authorization, timing in evidence (+14)
src/veyra_proof/tools/base.py                    cargo_command, nightly/stale detection,
                                                 ignored-test severity (+98)
src/veyra_proof/tools/cargo_check.py             lock policy + nightly fallback (+111)
src/veyra_proof/tools/clippy.py                  lock policy + nightly fallback (+97)
src/veyra_proof/tools/anti_theater.py            severity split, fuzz/ignore exclusions (+125)
src/veyra_proof/tools/audit.py                   lock re-checked at run time (+48)
src/veyra_proof/tools/e2e.py                     real duration (+25)
src/veyra_proof/tools/reproducible_build.py      policy-gated severity, real duration (+55)
src/veyra_proof/tools/cargo_test.py, nextest.py  ignored-test severity, lock policy (+20)
.cursor/skills/…, Veyra/skills/…                 reference.md + one SKILL.md rule
```

22 files, +910/−133, plus 4 new test files (`tests/test_lockfile_policy.py`,
`tests/test_verdicts.py`, `tests/test_anti_theater_noise.py`, `tests/test_docs_match_code.py`).
Test count: 130 → **262**, all green.

## Benchmarks (demo_crate, warm)

| Profile | Wall | Change |
|---------|------|--------|
| `--dry-run` (fixed overhead) | 2.8s | unchanged |
| `quick` | 8.7s | was 10.2s |
| `standard` | 25.3s | was 26.8s |
| `hardened` | 43.7s | now includes `reproducible-build` |

Time accounting improved from 4.6s of 10.2s accounted to 6.9s of 8.7s.

## Not done

* Nothing was committed and nothing was pushed.
* The skill was **not** installed into this machine's harnesses
  (`install.py --check` reports 6 detected; writing them is your call).
* `cargo build` caching, build parallelism and cross-run result caching were considered
  and rejected for the reasons above, not overlooked.
