"""Safe subprocess execution without shell=True."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from veyra_proof.models import CommandResult

# Cargo subcommands that build the project and therefore write Cargo.lock when
# the manifest and the lockfile disagree. Every one of these is given --locked
# so a stale lockfile becomes an error the operator sees instead of a silent
# rewrite of their dependency state. Subcommands are read-only by nature
# (metadata, tree, fmt) or are cargo plugins that reject unknown flags
# (audit, deny, geiger, llvm-cov, semver-checks), so they are left alone.
LOCKFILE_WRITING_SUBCOMMANDS = frozenset(
    {
        "build",
        "check",
        "test",
        "bench",
        "run",
        "doc",
        "rustdoc",
        "rustc",
        "fix",
        # Cargo components and plugins that forward to `cargo build` and accept
        # --locked in the subcommand's own argument position.
        "clippy",
        "nextest",
    }
)

# A flag that already decides the question; adding --locked would conflict.
LOCK_DECIDING_FLAGS = ("--locked", "--frozen", "--offline")

# Version probes run nothing that could write a lockfile, and some cargo
# plugins reject --locked at the top level: `cargo nextest --locked --version`
# exits 2, so detection reported an installed cargo-nextest as missing.
VERSION_FLAGS = ("--version", "-V")

CARGO_EXECUTABLES = {"cargo", "cargo.exe"}


def _cargo_argv(command: Sequence[str]) -> int | None:
    """Index of the cargo subcommand, or None when this is not a cargo build."""
    if not command:
        return None
    if Path(command[0]).name.lower() not in CARGO_EXECUTABLES:
        return None
    for index, part in enumerate(command[1:], start=1):
        if part.startswith("+") or part.startswith("-"):
            # Toolchain override (+nightly) and global flags are not the
            # subcommand; a bare `--` here would mean a malformed invocation.
            if part == "--":
                return None
            continue
        return index
    return None


def apply_lock_policy(
    command: Sequence[str],
    *,
    cwd: str | Path | None,
    allow_update: bool = False,
) -> list[str]:
    """Add ``--locked`` to a cargo build command when a lockfile already exists.

    Returns the argv to actually execute. Three cases, and no fourth:

    * a Cargo.lock is present and updates were not authorized -> ``--locked``,
      so the lockfile cannot be rewritten behind the operator's back;
    * no Cargo.lock exists -> untouched, because creating one is not a mutation
      of existing state and cargo has no other option;
    * updates were authorized with --update-lockfile -> untouched, on purpose.

    The flag goes immediately before the subcommand's first option so that a
    plugin shaped like ``cargo nextest run --workspace`` keeps working.
    """
    argv = list(command)
    index = _cargo_argv(argv)
    if index is None:
        return argv
    subcommand = argv[index]
    if subcommand not in LOCKFILE_WRITING_SUBCOMMANDS:
        return argv
    if allow_update:
        return argv
    if any(flag in argv for flag in LOCK_DECIDING_FLAGS):
        return argv
    # Version flags only count before the harness separator: `cargo test --
    # --version` runs tests filtered by that name and still builds.
    try:
        harness = argv.index("--")
    except ValueError:
        harness = len(argv)
    if any(flag in argv[index + 1 : harness] for flag in VERSION_FLAGS):
        return argv
    workdir = Path(cwd).resolve() if cwd else Path.cwd()
    if not (workdir / "Cargo.lock").is_file():
        return argv
    insert_at = len(argv)
    for position in range(index + 1, len(argv)):
        if argv[position].startswith("-"):
            insert_at = position
            break
    return argv[:insert_at] + ["--locked"] + argv[insert_at:]


class CommandRunner:
    """Execute argv lists and capture full evidence."""

    def __init__(
        self,
        *,
        dry_run: bool = False,
        verbose: bool = False,
        allow_lockfile_update: bool = False,
    ) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.allow_lockfile_update = allow_lockfile_update
        self.executed: list[CommandResult] = []

    def preview(self, command: Sequence[str], *, cwd: str | Path | None = None) -> list[str]:
        """The argv this runner would execute, without running anything."""
        return apply_lock_policy(
            command, cwd=cwd, allow_update=self.allow_lockfile_update
        )

    def run(
        self,
        command: list[str],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = 60.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        stream: bool = False,
        stream_prefix: str = "",
    ) -> CommandResult:
        if not command:
            raise ValueError("command must be a non-empty list")
        if any(not isinstance(part, str) for part in command):
            raise TypeError("command parts must be strings")

        workdir = str(Path(cwd).resolve()) if cwd else str(Path.cwd())
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        if self.dry_run:
            result = CommandResult(
                command=list(self.preview(command, cwd=workdir)),
                cwd=workdir,
                exit_code=None,
                stdout="",
                stderr="dry-run: command not executed",
                duration_seconds=0.0,
                timed_out=False,
                error="dry-run",
            )
            self.executed.append(result)
            return result

        command = self.preview(command, cwd=workdir)
        start = time.perf_counter()
        try:
            if stream:
                result = self._run_streaming(
                    command,
                    workdir=workdir,
                    timeout=timeout,
                    env=merged_env,
                    stream_prefix=stream_prefix,
                    started=start,
                )
            else:
                # Rust writes UTF-8; the process locale on Windows is often cp1252
                # and a single undecodable byte killed the reader thread, which
                # left stdout/stderr as None. The tool then reported a verdict
                # backed by two empty logs. Decoding explicitly with a
                # replacement keeps every verdict attached to its real output.
                completed = subprocess.run(
                    list(command),
                    cwd=workdir,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    env=merged_env,
                    input=input_text,
                    shell=False,
                    check=False,
                )
                duration = time.perf_counter() - start
                result = CommandResult(
                    command=list(command),
                    cwd=workdir,
                    exit_code=completed.returncode,
                    stdout=completed.stdout or "",
                    stderr=completed.stderr or "",
                    duration_seconds=duration,
                    timed_out=False,
                    error=None,
                )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - start
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            result = CommandResult(
                command=list(command),
                cwd=workdir,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=True,
                error=f"timeout after {timeout}s",
            )
        except OSError as exc:
            duration = time.perf_counter() - start
            result = CommandResult(
                command=list(command),
                cwd=workdir,
                exit_code=None,
                stdout="",
                stderr=str(exc),
                duration_seconds=duration,
                timed_out=False,
                error=str(exc),
            )

        self.executed.append(result)
        if self.verbose and not stream:
            print(
                f"$ {' '.join(command)}  (cwd={workdir}, exit={result.exit_code}, timed_out={result.timed_out})",
                flush=True,
            )
        return result

    def _run_streaming(
        self,
        command: list[str],
        *,
        workdir: str,
        timeout: float | None,
        env: dict[str, str],
        stream_prefix: str,
        started: float,
        out: TextIO | None = None,
    ) -> CommandResult:
        """Run with live stdout/stderr (merged) while still capturing for evidence."""
        sink = out or sys.stderr
        print(f"$ {' '.join(command)}", file=sink, flush=True)
        proc = subprocess.Popen(
            list(command),
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            env=env,
            shell=False,
            bufsize=1,
        )
        assert proc.stdout is not None
        chunks: list[str] = []
        timed_out = False
        try:
            while True:
                if timeout is not None and (time.perf_counter() - started) > timeout:
                    proc.kill()
                    timed_out = True
                    break
                line = proc.stdout.readline()
                if line:
                    chunks.append(line)
                    if stream_prefix:
                        sink.write(f"{stream_prefix}{line}")
                    else:
                        sink.write(line)
                    sink.flush()
                elif proc.poll() is not None:
                    # Drain remainder
                    rest = proc.stdout.read()
                    if rest:
                        chunks.append(rest)
                        if stream_prefix:
                            sink.write(f"{stream_prefix}{rest}")
                        else:
                            sink.write(rest)
                        sink.flush()
                    break
                else:
                    time.sleep(0.05)
            exit_code = proc.wait(timeout=5) if not timed_out else None
            if timed_out:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                exit_code = None
        except Exception:
            proc.kill()
            raise

        duration = time.perf_counter() - started
        captured = "".join(chunks)
        return CommandResult(
            command=list(command),
            cwd=workdir,
            exit_code=exit_code,
            stdout=captured,
            stderr="",  # merged into stdout when streaming
            duration_seconds=duration,
            timed_out=timed_out,
            error=f"timeout after {timeout}s" if timed_out else None,
        )
