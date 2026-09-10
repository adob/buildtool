"""Memory accounting and scheduling tests with injected kernel data and budgets."""

import asyncio
import io
import unittest
from unittest.mock import patch

from memory import GIB, MemoryBudget, available_memory_bytes
from scheduler import BuildSession, Job


def memory_from_files(files: dict[str, str]) -> int | None:
    """Read accounting from files; unlisted kernel paths behave as unavailable."""
    def read_text(path: str) -> str:
        """Return the supplied text for path, or simulate an unreadable file."""
        if path not in files:
            raise PermissionError(path)
        return files[path]

    return available_memory_bytes(read_text)


class MemoryAccountingTests(unittest.TestCase):
    def test_available_ram_not_total_or_free(self) -> None:
        """Use reclaimable capacity, not installed RAM or just unused pages."""
        self.assertEqual(memory_from_files({
            '/proc/meminfo': 'MemTotal: 9000 kB\nMemFree: 100 kB\nMemAvailable: 3000 kB\n',
        }), 3000 * 1024)

    def test_unavailable_or_malformed_accounting(self) -> None:
        """Unknown capacity must retain CPU-based scheduling."""
        for contents in ('', 'MemTotal: 9000 kB', 'MemAvailable:', 'MemAvailable: invalid kB'):
            with self.subTest(contents=contents):
                self.assertIsNone(memory_from_files({'/proc/meminfo': contents}))
        self.assertIsNone(memory_from_files({}))

    def test_cgroup_ancestor_limits(self) -> None:
        """An unlimited leaf must still respect its parent and host limits."""
        files = {
            '/proc/meminfo': f'MemAvailable: {20 * GIB // 1024} kB',
            '/proc/self/cgroup': '1:net_cls:/\n0::/parent/leaf\n',
            '/sys/fs/cgroup/parent/leaf/memory.max': 'max',
            '/sys/fs/cgroup/parent/memory.max': str(8 * GIB),
            '/sys/fs/cgroup/parent/memory.current': str(3 * GIB),
            '/sys/fs/cgroup/memory.max': str(30 * GIB),
            '/sys/fs/cgroup/memory.current': str(10 * GIB),
        }
        self.assertEqual(memory_from_files(files), 5 * GIB)
        files['/proc/meminfo'] = f'MemAvailable: {GIB // 1024} kB'
        self.assertEqual(memory_from_files(files), GIB)
        files['/sys/fs/cgroup/parent/memory.current'] = str(9 * GIB)
        self.assertEqual(memory_from_files(files), 0)

    def test_cgroup_namespace_root_without_proc_memory(self) -> None:
        """Root membership can provide a budget even without readable meminfo."""
        self.assertEqual(memory_from_files({
            '/proc/self/cgroup': '0::/\n',
            '/sys/fs/cgroup/memory.max': str(4 * GIB),
            '/sys/fs/cgroup/memory.current': str(GIB),
        }), 3 * GIB)

    def test_budget_limits_and_peak_reserve(self) -> None:
        """Keep one compiler runnable while reserving estimated growth for peers."""
        budget = MemoryBudget(available=lambda: 5 * GIB)
        for requested, available, expected in ((8, 5 * GIB, 2), (1, 5 * GIB, 1),
                                                (8, 0, 1), (8, None, 8)):
            self.assertEqual(budget.job_limit(requested, available), expected)
        self.assertTrue(budget.permits(1))
        self.assertFalse(budget.permits(2))
        self.assertTrue(MemoryBudget(available=lambda: 0).permits(0))
        self.assertTrue(MemoryBudget(available=lambda: None).permits(100))
        with self.assertRaises(ValueError):
            MemoryBudget(bytes_per_job=0)


class MemorySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_compilation_reports_selected_limit_once(self) -> None:
        """Defer the memory-capped limit until compilation and print it only once."""
        for available, expected in ((5 * GIB, 2), (0, 1), (None, 8)):
            with self.subTest(available=available):
                output = io.StringIO()
                session = BuildSession(8, output, MemoryBudget(available=lambda: available))
                self.assertEqual(session.slots.limit, expected)
                self.assertEqual(output.getvalue(), '')

                async def compile_job(job: Job) -> None:
                    """Acquire job's compiler slot and emit a diagnostic."""
                    async with job.compiler_slot():
                        job.message('compiling')

                session.schedule('first', compile_job)
                session.schedule('second', compile_job)
                await session.finish()
                self.assertIn(f'Concurrency: {expected} compiler jobs', output.getvalue())
                self.assertIn('requested: 8', output.getvalue())
                self.assertIn('2 GB/job estimate', output.getvalue())
                if available is not None:
                    self.assertIn(f'{available / GIB:.0f} GB available', output.getvalue())
                self.assertEqual(output.getvalue().count('Concurrency:'), 1)
                self.assertLess(output.getvalue().index('Concurrency:'),
                                output.getvalue().index('compiling'))

    async def test_up_to_date_jobs_do_not_report_concurrency(self) -> None:
        """Checking jobs that need no compiler must leave the output empty."""
        output = io.StringIO()
        session = BuildSession(8, output, MemoryBudget(available=lambda: 5 * GIB))

        async def up_to_date(job: Job) -> None:
            """Finish job's dependency check without acquiring a compiler slot."""
            return

        session.schedule('cached', up_to_date)
        await session.finish()
        self.assertEqual(output.getvalue(), '')

    async def test_memory_recovery_wakes_queued_compiler(self) -> None:
        """A timer admits queued work after RAM recovers without any job exiting."""
        available = 8 * GIB
        session = BuildSession(4, io.StringIO(), MemoryBudget(available=lambda: available))
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        finish = asyncio.Event()

        async def first(job: Job) -> None:
            """Hold job's slot and simulate another process consuming RAM."""
            nonlocal available
            async with job.compiler_slot():
                available = GIB
                first_started.set()
                await finish.wait()

        async def second(job: Job) -> None:
            """Acknowledge job's admission while the first compiler is still active."""
            async with job.compiler_slot():
                second_started.set()
                await finish.wait()

        try:
            session.schedule('first', first)
            await asyncio.wait_for(first_started.wait(), 2)
            # Capture the scheduled retry so the test advances it without sleeps.
            loop = asyncio.get_running_loop()
            with patch.object(loop, 'call_later', wraps=loop.call_later) as timers:
                session.schedule('second', second)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            self.assertFalse(second_started.is_set())
            self.assertIsNotNone(session.slots.retry)
            delay, retry = timers.call_args.args
            self.assertEqual(delay, 0.5)
            available = 8 * GIB
            retry()
            await asyncio.wait_for(second_started.wait(), 2)
            self.assertFalse(session.jobs['first'].finished)
            finish.set()
            await session.finish()
        finally:
            await session.close()

    async def test_nested_imports_progress_with_no_available_memory(self) -> None:
        """Lend slots to nested modules and resume importers even below the estimate."""
        session = BuildSession(8, io.StringIO(), MemoryBudget(available=lambda: 0))
        visited = []

        async def compile_job(job: Job) -> None:
            """Recursively import a child from job while holding a compiler slot."""
            async with job.compiler_slot():
                if job.key < 3:
                    child = session.schedule(job.key + 1, compile_job, parent=job)
                    await job.wait_for_dependency(child)
                self.assertTrue(job.has_slot)
                visited.append(job.key)

        session.schedule(0, compile_job)
        await asyncio.wait_for(session.finish(), 2)
        self.assertEqual(visited, [3, 2, 1, 0])

    async def test_shutdown_cancels_memory_polling_and_waiters(self) -> None:
        """A blocked memory admission must not outlive the session."""
        available = 8 * GIB
        session = BuildSession(4, io.StringIO(), MemoryBudget(available=lambda: available))
        started = asyncio.Event()

        async def hold(job: Job) -> None:
            """Keep job's compiler slot occupied until cancellation."""
            async with job.compiler_slot():
                started.set()
                await asyncio.Event().wait()

        try:
            session.schedule('first', hold)
            await asyncio.wait_for(started.wait(), 2)
            available = 0
            session.schedule('queued', hold)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            timer = session.slots.retry
            self.assertIsNotNone(timer)
            await session.close()
            self.assertTrue(timer.cancelled())
            self.assertIsNone(session.slots.retry)
            self.assertFalse(session.slots.waiters)
            self.assertTrue(all(job.task.cancelled() for job in session.jobs.values()))
        finally:
            await session.close()
