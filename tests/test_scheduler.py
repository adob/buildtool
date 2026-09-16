"""Scheduling and streaming tests use events instead of timing assumptions."""

import asyncio
import io
import unittest

from scheduler import BuildSession, Job


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_jobs_run_early_but_report_after_dependencies(self) -> None:
        """Final output follows compilation logs even when a linker finishes first."""
        output = io.StringIO()
        session = BuildSession(jobs=2, output=output)
        linked = asyncio.Event()

        async def fast(job: Job) -> None:
            """Complete the first target's root."""
            job.message('fast')

        async def child(job: Job) -> None:
            """Keep the second target's discovered dependency running until linking."""
            await linked.wait()
            job.message('child')

        async def slow(job: Job) -> None:
            """Discover a dependency whose output must precede link diagnostics."""
            session.schedule('child', child, parent=job)
            job.message('slow')

        async def link(job: Job) -> None:
            """Link the ready target without waiting for the second target's dependency."""
            await first.task
            job.message('link')
            linked.set()

        first = session.schedule('fast', fast)
        session.schedule('slow', slow)
        session.schedule('link', link, final=True)
        await asyncio.wait_for(session.finish(), 2)
        self.assertEqual(output.getvalue(), 'fast\nslow\nchild\nlink\n')

    async def test_output_is_identical_when_completion_order_reverses(self):
        """Change which sibling first creates a shared job without changing logs."""
        outputs = []
        for order in (('a', 'b'), ('b', 'a')):
            output = io.StringIO()
            session = BuildSession(jobs=2, output=output)
            gates = {name: asyncio.Event() for name in order}
            siblings = {}

            async def shared(job):
                """Emit the common dependency's output once."""
                job.message('shared')

            async def sibling(job):
                """Use this run's gate to control when job discovers its import."""
                await gates[job.key].wait()
                await job.wait_for_dependency(session.schedule('shared', shared, parent=job))
                job.message(job.key + ': warning')

            async def root(job):
                """Record siblings in a fixed order independently of their gates."""
                for name in ('a', 'b'):
                    siblings[name] = session.schedule(name, sibling, parent=job)
                job.message('root')

            root_job = session.schedule('root', root)

            async def release():
                """Finish siblings in the order selected for this test iteration."""
                await root_job.task
                for name in order:
                    gates[name].set()
                    await siblings[name].task

            await asyncio.wait_for(asyncio.gather(session.finish(), release()), 2)
            outputs.append(output.getvalue())
        self.assertEqual(outputs, ['root\na: warning\nb: warning\nshared\n'] * 2)

    async def test_streams_before_completion_and_preserves_breadth_first_order(self):
        """Acknowledge root output before allowing the root action to finish."""
        printed = asyncio.Event()

        class Output(io.StringIO):
            def write(self, text):
                """Record text and acknowledge the root's first streamed line."""
                result = super().write(text)
                if 'root starts' in self.getvalue():
                    printed.set()
                return result

        output = Output()
        session = BuildSession(jobs=2, output=output)
        right_finished = asyncio.Event()

        async def leaf(job):
            """Write the shared leaf job's diagnostic."""
            job.message('leaf')

        async def left(job):
            """Finish after right, despite preceding it in discovery order."""
            await right_finished.wait()
            dependency = session.schedule('leaf', leaf, parent=job)
            await job.wait_for_dependency(dependency)
            job.message('left: warning')

        async def right(job):
            """Discover the shared leaf first and finish before left."""
            dependency = session.schedule('leaf', leaf, parent=job)
            await job.wait_for_dependency(dependency)
            job.message('right')
            right_finished.set()

        async def root(job):
            """Schedule siblings, then prove output streams while still running."""
            session.schedule('left', left, parent=job)
            session.schedule('right', right, parent=job)
            job.write('root starts\n')
            await printed.wait()
            job.write('root ends\n')

        session.schedule('root', root)
        await asyncio.wait_for(session.finish(), 2)
        self.assertEqual(output.getvalue(),
                         'root starts\nroot ends\nleft: warning\nright\nleaf\n')

    async def test_waiting_compilers_release_slots_and_share_dependency(self):
        """Saturate two slots, then require the same third compiler from both."""
        session = BuildSession(jobs=2, output=io.StringIO())
        both_running = asyncio.Event()
        started = []
        leaf_calls = []

        async def leaf(job):
            """Acquire a slot released by one of the two blocked importers."""
            async with job.compiler_slot():
                leaf_calls.append(job.key)

        async def importer(job):
            """Synchronize both active jobs before requesting the shared leaf."""
            async with job.compiler_slot():
                started.append(job.key)
                if len(started) == 2:
                    both_running.set()
                await both_running.wait()
                await job.wait_for_dependency(session.schedule('leaf', leaf, parent=job))
                self.assertTrue(job.has_slot)

        session.schedule('a', importer)
        session.schedule('b', importer)
        await asyncio.wait_for(session.finish(), 2)
        self.assertEqual(leaf_calls, ['leaf'])

    async def test_failure_stops_output_and_cancels_remaining_jobs(self):
        """A later job's buffered output must never follow the current failure."""
        output = io.StringIO()
        session = BuildSession(jobs=2, output=output)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fail(job):
            """Fail only after the later job has produced buffered output."""
            await started.wait()
            job.message('compiler error')
            raise RuntimeError('failed')

        async def later(job):
            """Produce hidden output and remain active until cancellation."""
            job.message('must not appear')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        session.schedule('fail', fail)
        session.schedule('later', later)
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            await asyncio.wait_for(session.finish(), 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(output.getvalue(), 'compiler error\nbuildtool: error: failed\n')

    async def test_later_failure_does_not_hide_earlier_warnings(self):
        """A failure that finishes early waits behind earlier jobs in the queue."""
        output = io.StringIO()
        session = BuildSession(output=output)
        failure_finished = asyncio.Event()

        async def warning(job):
            """Emit the earlier job's warning after the later failure is known."""
            await failure_finished.wait()
            job.message('first: warning')

        async def failure(job):
            """Fail before the earlier warning job finishes."""
            failure_finished.set()
            raise RuntimeError('second: error')

        session.schedule('warning', warning)
        session.schedule('failure', failure)
        with self.assertRaisesRegex(RuntimeError, 'second: error'):
            await session.finish()
        self.assertEqual(output.getvalue(), 'first: warning\nbuildtool: error: second: error\n')

    async def test_cycle_reports_error_instead_of_deadlocking(self):
        """A -> B -> A is a cycle; unrelated shared dependencies are not."""
        session = BuildSession(output=io.StringIO())

        async def a(job):
            """Require B from A."""
            await job.wait_for_dependency(session.schedule('b', b, parent=job))

        async def b(job):
            """Require the already running A from B."""
            await job.wait_for_dependency(session.schedule('a', a, parent=job))

        session.schedule('a', a)
        with self.assertRaisesRegex(RuntimeError, 'Cyclic module import'):
            await asyncio.wait_for(session.finish(), 2)

    async def test_dependency_failure_keeps_underlying_diagnostic(self):
        """Stopping at a failed importer must still explain the module failure."""
        output = io.StringIO()
        session = BuildSession(output=output)

        async def dependency(job):
            """Emit the diagnostic that caused the importer to fail."""
            job.message('header.h:17: invalid declaration')
            raise RuntimeError('header compilation failed')

        async def root(job):
            """Wait for the failing header unit."""
            await job.wait_for_dependency(session.schedule('header.h', dependency, parent=job))

        session.schedule('root', root)
        with self.assertRaisesRegex(RuntimeError, 'header compilation failed'):
            await session.finish()
        self.assertIn('header.h:17: invalid declaration', output.getvalue())

    def test_invalid_job_limit(self):
        """Reject a zero slot budget rather than hanging the first compiler."""
        with self.assertRaises(ValueError):
            BuildSession(jobs=0)
