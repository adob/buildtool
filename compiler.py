"""Async subprocess execution; diagnostics belong to a single scheduler job."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import BinaryIO, TextIO
import os
import shlex
import signal
import subprocess

if __package__:
    from .scheduler import Job
else:
    from scheduler import Job


async def read_output(
    stream: asyncio.StreamReader,
    job: Job,
    captured: bytearray | None = None,
    command: str | None = None,
) -> None:
    """Drain stream into job or captured; prefix displayed output with command once."""
    while data := await stream.read(65536):
        if captured is None:
            if command is not None:
                job.message(command)
                command = None
            job.write(data)
        else:
            captured.extend(data)


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Terminate process and its compiler children, then reap the leader."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass
    # The driver may exit before a child that ignored SIGTERM. Kill the group
    # even when the driver's own wait() has already completed.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


def diagnostic_color_flags(output: TextIO, command: Sequence[str]) -> list[str]:
    """Force color for terminal output unless command or environment opts out."""
    if not output.isatty() or os.environ.get('TERM') == 'dumb' or os.environ.get('NO_COLOR'):
        return []
    color_options = ('-fdiagnostics-color', '-fno-diagnostics-color',
                     '-fcolor-diagnostics', '-fno-color-diagnostics')
    if any(arg.split('=', 1)[0] in color_options for arg in command):
        return []
    # The compiler sees our capture pipe, so its automatic TTY detection cannot
    # see that the reporter ultimately writes to a terminal.
    return ['-fdiagnostics-color=always']


async def run_compiler(
    job: Job,
    command: Sequence[str | os.PathLike[str]],
    *,
    protocol: Callable[[asyncio.subprocess.Process], Awaitable[None]] | None = None,
    pass_fds: Sequence[int] = (),
    env: Mapping[str, str] | None = None,
    capture: bool = False,
    color_diagnostics: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run command in job; protocol handles mapper requests while output drains.

    pass_fds and env configure the child. capture returns stdout/stderr bytes
    without checking status, for tools whose output is machine-readable.
    color_diagnostics enables terminal-aware GCC/Clang diagnostic flags.
    """
    command = list(map(str, command))
    if color_diagnostics:
        command += diagnostic_color_flags(job.session.output, command)
    stdout, stderr = bytearray(), bytearray()
    async with job.compiler_slot():
        command_text = shlex.join(command)
        if job.session.verbose:
            job.session.report_launch(command_text)
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if capture else asyncio.subprocess.STDOUT,
            pass_fds=pass_fds, env=env, start_new_session=True)
        readers = [asyncio.create_task(read_output(
            process.stdout, job, stdout if capture else None,
            None if job.session.verbose else command_text))]
        if capture:
            readers.append(asyncio.create_task(read_output(process.stderr, job, stderr)))
        try:
            if protocol:
                await protocol(process)
            code = await process.wait()
            await asyncio.gather(*readers)
            if not capture and code:
                raise subprocess.CalledProcessError(code, command)
            return subprocess.CompletedProcess(command, code, bytes(stdout), bytes(stderr))
        finally:
            if process.returncode is None or any(not task.done() for task in readers):
                await stop_process(process)
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)


@asynccontextmanager
async def mapper_pipe() -> AsyncIterator[tuple[asyncio.StreamReader, TextIO, BinaryIO, BinaryIO]]:
    """Yield reader, reply file and child pipe files; close child files at spawn."""
    parent_read, child_write = os.pipe()
    child_read, parent_write = os.pipe()
    transport = None
    input_file = os.fdopen(parent_read, 'rb', buffering=0)
    reply_file = os.fdopen(parent_write, 'w')
    child_input = os.fdopen(child_read, 'rb', buffering=0)
    child_output = os.fdopen(child_write, 'wb', buffering=0)
    try:
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), input_file)
        yield reader, reply_file, child_input, child_output
    finally:
        if transport:
            transport.close()
        else:
            input_file.close()
        reply_file.close()
        child_input.close()
        child_output.close()
