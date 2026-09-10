"""Cooperative build jobs with breadth-first, uninterrupted output streams."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from typing import TextIO
from contextlib import asynccontextmanager
import sys
import tempfile

if __package__:
    from .memory import GIB, MemoryBudget
else:
    from memory import GIB, MemoryBudget


class CompilerSlots:
    def __init__(self, count: int, memory: MemoryBudget | None = None) -> None:
        """Provide count slots; optional memory gates launches and resumptions."""
        self.limit = count
        self.available = count
        self.memory = memory
        self.retry: asyncio.TimerHandle | None = None
        self.closed = False
        self.waiters = []

    async def acquire(self, job: Job, resuming: bool = False) -> None:
        """Wait for a slot for job; resuming means its compiler already exists."""
        future = asyncio.get_running_loop().create_future()
        entry = (job, resuming, future)
        self.waiters.append(entry)
        # Dispatch on the next loop turn so newly discovered dependencies can
        # take precedence over unrelated sources that were already queued.
        asyncio.get_running_loop().call_soon(self.dispatch)
        try:
            await future
        except asyncio.CancelledError:
            if entry in self.waiters:
                self.waiters.remove(entry)
            elif not future.cancelled():
                self.release()
            raise

    def release(self) -> None:
        """Return an execution slot and wake queued compilers."""
        self.available += 1
        if not self.closed:
            asyncio.get_running_loop().call_soon(self.dispatch)

    def dispatch(self) -> None:
        """Grant available slots in priority order, retaining FIFO within a priority."""
        if self.retry is not None:
            self.retry.cancel()
            self.retry = None
        if self.closed:
            return
        self.waiters = [entry for entry in self.waiters if not entry[2].done()]
        while self.available and self.waiters:
            if self.memory is not None and not self.memory.permits(self.limit - self.available):
                # External processes can release RAM without a compiler exiting.
                self.retry = asyncio.get_running_loop().call_later(0.5, self.dispatch)
                break
            index = min(range(len(self.waiters)), key=lambda i:
                        (not (self.waiters[i][0].required or self.waiters[i][1]), i))
            _, _, future = self.waiters.pop(index)
            if future.done():
                continue
            self.available -= 1
            future.set_result(None)

    def close(self) -> None:
        """Stop memory polling when the owning session shuts down."""
        self.closed = True
        if self.retry is not None:
            self.retry.cancel()
            self.retry = None


class Job:
    def __init__(self, session: BuildSession, key: Hashable, work: Callable[[Job], Awaitable[None]]) -> None:
        """Create a job identified by key; work(job) is its async build action."""
        self.session = session
        self.key = key
        self.work = work
        self.children = []
        self.waiting_on = set()
        self.has_slot = False
        self.required = False
        self.error = None
        self.finished = False
        self.changed = asyncio.Event()
        self.log = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        self.size = 0
        self.task = asyncio.create_task(self.run())

    def write(self, data: str | bytes) -> None:
        """Append bytes or text data to this job's stream and wake its reader."""
        if isinstance(data, str):
            data = data.encode('utf-8', errors='replace')
        self.log.seek(self.size)
        self.log.write(data)
        self.size += len(data)
        self.changed.set()

    def message(self, *parts: object) -> None:
        """Append a newline-terminated message made from the supplied parts."""
        self.write(' '.join(map(str, parts)) + '\n')

    async def run(self) -> None:
        """Execute the build action, recording failure without losing its log."""
        try:
            await self.work(self)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error
            self.message(error)
        finally:
            self.finished = True
            self.changed.set()

    async def wait_for_dependency(self, dependency: Job) -> None:
        """Suspend this importer until dependency finishes, lending its compiler slot."""
        dependency.required = True
        pending = [dependency]
        seen = set()
        while pending:
            current = pending.pop()
            if current is self:
                raise RuntimeError(f'Cyclic module import: {self.key} -> {dependency.key}')
            if current not in seen:
                seen.add(current)
                pending.extend(current.waiting_on)
        self.waiting_on.add(dependency)
        resume = self.has_slot
        if resume:
            self.session.slots.release()
            self.has_slot = False
        try:
            # Shield the shared task: cancellation of one importer must not
            # implicitly cancel work that another importer also needs.
            await asyncio.shield(dependency.task)
            if dependency.error:
                # The reporter may stop at this importer, before visiting the
                # dependency. Preserve the underlying diagnostic in that case.
                self.message(f'Dependency {dependency.key} failed:')
                dependency.copy_log(self)
                raise dependency.error
        finally:
            self.waiting_on.remove(dependency)
        if resume:
            await self.session.slots.acquire(self, resuming=True)
            self.has_slot = True

    def copy_log(self, destination: Job) -> None:
        """Copy this completed job's diagnostic stream into destination job."""
        self.log.seek(0)
        while data := self.log.read(65536):
            destination.write(data)

    @asynccontextmanager
    async def compiler_slot(self) -> AsyncIterator[None]:
        """Hold an execution slot until the compiler exits or awaits a module."""
        await self.session.slots.acquire(self)
        self.has_slot = True
        try:
            self.session.compilation_started = True
            yield
        finally:
            if self.has_slot:
                self.has_slot = False
                self.session.slots.release()


class ConcurrencyReporter:
    def __init__(self) -> None:
        """Track whether this invocation has printed its concurrency banner."""
        self.reported = False

    def write(self, message: str, output: TextIO) -> None:
        """Write message to output once across all sessions sharing this reporter."""
        if not self.reported:
            output.write(message)
            self.reported = True


class BuildSession:
    def __init__(
        self,
        jobs: int = 1,
        output: TextIO | None = None,
        memory: MemoryBudget | None = None,
        verbose: bool = False,
        concurrency_reporter: ConcurrencyReporter | None = None,
    ) -> None:
        """Limit jobs by memory; output logs and share an optional invocation reporter."""
        if jobs < 1:
            raise ValueError('jobs must be at least 1')
        self.output = output if output is not None else sys.stdout
        self.verbose = verbose
        self.concurrency_reporter = (concurrency_reporter if concurrency_reporter is not None
                                     else ConcurrencyReporter())
        limit = jobs
        self.concurrency_message = ''
        self.compilation_started = False
        if memory is not None:
            available = memory.available()
            limit = memory.job_limit(jobs, available)
            memory_text = ('available memory unknown' if available is None else
                           f'{available / GIB:.0f} GB available')
            self.concurrency_message = (
                f'Concurrency: {limit} compiler jobs '
                f'(requested: {jobs}, {memory_text}, '
                f'{memory.bytes_per_job / GIB:.0f} GB/job estimate)\n')
        self.slots = CompilerSlots(limit, memory)
        self.jobs = {}
        self.roots = []

    def report_concurrency(self) -> None:
        """Print the banner once before streamed output, after compilation starts."""
        if self.compilation_started and self.concurrency_message:
            self.concurrency_reporter.write(self.concurrency_message, self.output)
            self.concurrency_message = ''

    def report_launch(self, command: str) -> None:
        """Print command immediately, bypassing job logs, and flush the output stream."""
        self.report_concurrency()
        print(f'launching {command}', file=self.output, flush=True)

    def schedule(
        self,
        key: Hashable,
        work: Callable[[Job], Awaitable[None]],
        parent: Job | None = None,
    ) -> Job:
        """Share work(job) by key; record every parent's encounter order."""
        if key not in self.jobs:
            self.jobs[key] = Job(self, key, work)
        job = self.jobs[key]
        children = self.roots if parent is None else parent.children
        if job not in children:
            children.append(job)
        return job

    async def stream(self, job: Job) -> None:
        """Print job's buffered bytes, then new bytes until the job completes."""
        offset = 0
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while True:
            job.changed.clear()
            job.log.seek(offset)
            while data := job.log.read(65536):
                offset += len(data)
                self.report_concurrency()
                self.output.write(decoder.decode(data))
                self.output.flush()
            if job.finished:
                self.output.write(decoder.decode(b'', final=True))
                self.output.flush()
                return
            await job.changed.wait()

    async def finish(self) -> None:
        """Report the breadth-first queue, stopping and cancelling on failure."""
        queue = deque(self.roots)
        printed = set()
        try:
            while queue:
                job = queue.popleft()
                if job in printed:
                    continue
                printed.add(job)
                await self.stream(job)
                if job.error:
                    job.error.buildtool_reported = True
                    raise job.error
                queue.extend(job.children)
        finally:
            await self.close()

    async def close(self) -> None:
        """Cancel/reap unfinished work and close logs; safe to call more than once."""
        self.slots.close()
        for job in self.jobs.values():
            if not job.task.done():
                job.task.cancel()
        await asyncio.gather(*(job.task for job in self.jobs.values()),
                             return_exceptions=True)
        for job in self.jobs.values():
            job.log.close()
