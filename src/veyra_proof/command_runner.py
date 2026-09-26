"""Safe subprocess execution without shell=True."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, TextIO

from veyra_proof.models import CommandResult


class CommandRunner:
    """Execute argv lists and capture full evidence."""

    def __init__(self, *, dry_run: bool = False, verbose: bool = False) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.executed: list[CommandResult] = []

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
                command=list(command),
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

        if stream and input_text is not None:
            raise ValueError("stream=True cannot be combined with input_text")

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
                completed = subprocess.run(
                    list(command),
                    cwd=workdir,
                    capture_output=True,
                    text=True,
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
            text=True,
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
