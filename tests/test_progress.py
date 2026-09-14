"""Verify nested progress while compilation and dependency discovery remain asynchronous."""

import asyncio
import io
import unittest

from scheduler import BuildSession, Job
from memory import GIB, MemoryBudget


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_indented_concurrency(self) -> None:
        """Indent the worker ceiling, requested count, rounded memory, and job estimate."""
        for available, details in (
                (int(10.6 * GIB), '5; requested 8; 11 GB available; 2 GB/job estimate'),
                (None, '8; requested 8; available memory unknown; 2 GB/job estimate')):
            with self.subTest(available=available):
                output = io.StringIO()
                session = BuildSession(jobs=8, output=output, progress=True,
                                       memory=MemoryBudget(available=lambda: available))

                async def work(job: Job) -> None:
                    """Complete one compilation to trigger the concurrency line."""
                    job.start_compilation('module example.cc')
                    job.complete_compilation()

                session.schedule('example', work)
                await session.finish()
                self.assertTrue(output.getvalue().startswith(
                    f'       buildtool concurrency: {details}\n'))
                self.assertNotIn('Concurrency:', output.getvalue())
                self.assertEqual(output.getvalue().count('buildtool concurrency:'), 1)

    async def test_live_progress_and_discovered_dependency(self) -> None:
        """Print immediately, grow the total for imports, and keep diagnostic streams ordered."""
        printed = asyncio.Event()

        class Output(io.StringIO):
            def write(self, text: str) -> int:
                """Notify the test when the first progress line reaches the output."""
                result = super().write(text)
                if '[0/1] Building' in text:
                    printed.set()
                return result

        output = Output()
        session = BuildSession(jobs=2, output=output, progress=True)

        async def cached(job: Job) -> None:
            """Represent a cache hit, which must not contribute to compilation counts."""
            pass

        async def child(job: Job) -> None:
            """Compile a newly discovered dependency before its importing parent finishes."""
            job.start_compilation('module dependency.cc')
            job.message('child warning')
            job.complete_compilation()

        async def parent(job: Job) -> None:
            """Wait for live reporting, then discover and await a shared child."""
            job.start_compilation('module parent.cc')
            await printed.wait()
            dependency = session.schedule('child', child, parent=job)
            self.assertIs(dependency, session.schedule('child', child, parent=job))
            await job.wait_for_dependency(dependency)
            job.message('parent warning')
            job.complete_compilation()

        session.schedule('cached', cached)
        session.schedule('parent', parent)
        await asyncio.wait_for(session.finish(), 2)
        self.assertEqual(output.getvalue(),
                         '       [0/1] Building module parent.cc\n'
                         'parent warning\n'
                         '       [2/2] Building module dependency.cc\n'
                         'child warning\n')
        self.assertEqual(session.compilations_completed, 2)
        self.assertEqual(session.compilations_total, 2)

    async def test_failed_compilation_is_not_completed(self) -> None:
        """Preserve failure diagnostics without counting failed work as successful."""
        output = io.StringIO()
        session = BuildSession(output=output, progress=True)

        async def fail(job: Job) -> None:
            """Register a compilation that fails before successful completion."""
            job.start_compilation('module broken.cc')
            raise RuntimeError('compiler failed')

        session.schedule('failure', fail)
        with self.assertRaisesRegex(RuntimeError, 'compiler failed'):
            await session.finish()
        self.assertEqual(session.compilations_completed, 0)
        self.assertEqual(output.getvalue(),
                         '       [0/1] Building module broken.cc\ncompiler failed\n')

    async def test_up_to_date_build_is_silent(self) -> None:
        """Cache checks produce neither progress descriptions nor counts."""
        output = io.StringIO()
        session = BuildSession(output=output, progress=True,
                               memory=MemoryBudget(available=lambda: 8 * GIB))

        async def cached(job: Job) -> None:
            """Finish without launching compilation."""
            pass

        session.schedule('cached', cached)
        await session.finish()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(session.compilations_total, 0)
