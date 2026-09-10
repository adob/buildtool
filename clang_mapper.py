"""JSON-line transport to the patched Clang frontend wrapper."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import os
import inspect
import json
import shutil

MapperResult = str | os.PathLike[str] | dict[str, object]
if __package__:
    from .compiler import mapper_pipe, run_compiler
    from .scheduler import BuildSession, Job
else:
    from compiler import mapper_pipe, run_compiler
    from scheduler import BuildSession, Job


async def compile_with_mapper_async(
    job: Job,
    wrapper: str | os.PathLike[str],
    command: Sequence[str | os.PathLike[str]],
    resolve: Callable[[object], Awaitable[MapperResult]],
) -> None:
    """Run wrapper/command in job; await resolve(request) for each PCM path."""
    command = list(map(str, command))
    command[0] = shutil.which(command[0]) or command[0]
    async with mapper_pipe() as (requests, replies, child_input, child_output):
        read_fd, write_fd = child_input.fileno(), child_output.fileno()
        wrapper_command = [str(wrapper), "--mapper-fds", str(read_fd),
                           str(write_fd), "--", *command]

        async def protocol(process: asyncio.subprocess.Process) -> None:
            """Serve process's requests; close parent copies so EOF is observable."""
            child_input.close()
            child_output.close()
            while line := await requests.readline():
                try:
                    result = await resolve(json.loads(line))
                    reply = result if isinstance(result, dict) else {"pcm": str(result)}
                except Exception as error:
                    try:
                        replies.write(json.dumps({"error": str(error)}) + "\n")
                        replies.flush()
                    except BrokenPipeError:
                        pass
                    raise
                replies.write(json.dumps(reply) + "\n")
                replies.flush()

        await run_compiler(job, wrapper_command, protocol=protocol,
                           pass_fds=(read_fd, write_fd), color_diagnostics=True)


def compile_with_mapper(
    wrapper: str | os.PathLike[str],
    command: Sequence[str | os.PathLike[str]],
    resolve: Callable[[object], MapperResult | Awaitable[MapperResult]],
) -> None:
    """Synchronous entry point; resolve(request) may return a path or awaitable."""
    async def run() -> None:
        """Create a one-job session for this standalone mapper invocation."""
        session = BuildSession()

        async def resolve_async(request: object) -> MapperResult:
            """Adapt this caller's resolver for the asynchronous transport."""
            result = resolve(request)
            return await result if inspect.isawaitable(result) else result

        async def work(job: Job) -> None:
            """Execute the supplied wrapper command within job's output stream."""
            await compile_with_mapper_async(job, wrapper, command, resolve_async)

        session.schedule(str(command), work)
        await session.finish()

    asyncio.run(run())
