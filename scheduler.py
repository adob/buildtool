"""Cooperative build jobs with breadth-first, uninterrupted output streams."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from typing import TextIO
from contextlib import asynccontextmanager
import sys
import os
import tempfile

if __package__:
    from .jobserver import JobServer
    from .memory import GIB, MemoryBudget
else:
    from jobserver import JobServer
    from memory import GIB, MemoryBudget


class CompilerSlots:
    def __init__(self, count: int, memory: MemoryBudget | None = None,
                 jobserver: JobServer | None = None) -> None:
        """Provide count slots, additionally gated by memory and shared jobserver tokens."""
        self.limit = count
        self.available = count
        self.memory = memory
        self.jobserver = jobserver
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
        if self.jobserver is not None:
            self.jobserver.release()
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
            job, resuming, future = self.waiters.pop(index)
            if future.done():
                continue
            if self.jobserver is not None:
                try:
                    acquired = self.jobserver.try_acquire()
                except OSError as error:
                    future.set_exception(error)
                    continue
                if not acquired:
                    self.waiters.insert(index, (job, resuming, future))
                    self.retry = asyncio.get_running_loop().call_later(0.05, self.dispatch)
                    break
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
        self.compilation_description: str | None = None
        self.task = asyncio.create_task(self.run())

    def start_compilation(self, description: str) -> None:
        """Register actual compilation work described by description, excluding cache hits."""
        self.session.compilation_started = True
        self.session.compilations_total += 1
        self.compilation_description = description
        self.changed.set()

    def complete_compilation(self) -> None:
        """Count this compilation after its compiler has completed successfully."""
        self.session.compilations_completed += 1

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
    async def compiler_slot(self, *, compilation: bool = True) -> AsyncIterator[None]:
        """Hold a slot; compilation marks compiler work rather than linking/inspection."""
        await self.session.slots.acquire(self)
        self.has_slot = True
        try:
            if compilation:
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
        jobserver: JobServer | None = None,
        progress: bool = False,
    ) -> None:
        """Limit jobs; progress enables indented concurrency and numbered descriptions."""
        if jobs < 1:
            raise ValueError('jobs must be at least 1')
        self.output = output if output is not None else sys.stdout
        self.verbose = verbose
        self.progress = progress
        self.compilations_total = 0
        self.compilations_completed = 0
        self.concurrency_reporter = (concurrency_reporter if concurrency_reporter is not None
                                     else ConcurrencyReporter())
        limit = jobs
        if jobserver is not None and jobserver.jobs is not None:
            limit = min(limit, jobserver.jobs)
        self.concurrency_message = ''
        self.compilation_started = False
        if memory is not None:
            available = memory.available()
            limit = memory.job_limit(limit, available)
            memory_text = ('available memory unknown' if available is None else
                           f'{available / GIB:.0f} GB available')
            if progress:
                requested = jobs if jobserver is None else jobserver.jobs
                requested_text = str(requested) if requested is not None else 'unknown'
                self.concurrency_message = (
                    f'       buildtool concurrency: {limit}; requested {requested_text}; '
                    f'{memory_text}; {memory.bytes_per_job / GIB:.0f} GB/job estimate\n')
            else:
                limit_text = f'up to {limit}' if jobserver is not None else str(limit)
                request_text = f'requested: {jobs}'
                if jobserver is not None:
                    request_text = (f'requested: {jobserver.jobs}' if jobserver.jobs is not None
                                    else f'local limit: {jobs}')
                self.concurrency_message = (
                    f'Concurrency: {limit_text} compiler jobs '
                    f'({request_text}, {memory_text}, '
                    f'{memory.bytes_per_job / GIB:.0f} GB/job estimate)')
                if jobserver is not None:
                    self.concurrency_message += ' [shared jobserver]'
                self.concurrency_message += '\n'
        self.slots = CompilerSlots(limit, memory, jobserver)
        self.jobs = {}
        self.roots = []
        self.final_jobs: list[Job] = []

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
        *,
        final: bool = False,
    ) -> Job:
        """Share work(job) by key; final jobs run now but report after compilation logs."""
        if final and parent is not None:
            raise ValueError('Final jobs cannot have a parent')
        if key not in self.jobs:
            self.jobs[key] = Job(self, key, work)
        job = self.jobs[key]
        children = self.final_jobs if final else (self.roots if parent is None else parent.children)
        if job not in children:
            children.append(job)
        return job

    async def stream(self, job: Job) -> None:
        """Print job's buffered bytes, then new bytes until the job completes."""
        offset = 0
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        progress_printed = False
        while True:
            job.changed.clear()
            if self.progress and job.compilation_description is not None and not progress_printed:
                self.report_concurrency()
                message = (f'       [{self.compilations_completed}/{self.compilations_total}] '
                           f'Building {job.compilation_description}')
                if (self.output.isatty() and os.environ.get('TERM') != 'dumb'
                        and not os.environ.get('NO_COLOR')):
                    message = f'\x1b[32m{message}\x1b[0m'
                self.output.write(message + '\n')
                self.output.flush()
                progress_printed = True
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

    async def finish(self, *, keep_going: bool = False) -> None:
        """Report the queue; keep_going lets independent test packages finish after errors."""
        queue = deque(self.roots)
        final_queue = deque(self.final_jobs)
        printed = set()
        try:
            while queue or final_queue:
                job = queue.popleft() if queue else final_queue.popleft()
                if job in printed:
                    continue
                printed.add(job)
                await self.stream(job)
                if job.error:
                    job.error.buildtool_reported = True
                    if not keep_going:
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
