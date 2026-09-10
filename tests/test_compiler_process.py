"""Exercise output draining and cancellation with real, tiny child processes."""

import asyncio
import io
import os
import sys
import unittest

from compiler import mapper_pipe, run_compiler
from scheduler import BuildSession


class CompilerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_output_is_drained_while_waiting_for_mapper(self):
        """A child can fill both output streams before asking for a dependency."""
        output = io.StringIO()
        session = BuildSession(output=output)

        async def work(job):
            """Run a child whose output exceeds pipe capacity before its request."""
            async with mapper_pipe() as (requests, replies, child_input, child_output):
                read_fd, write_fd = child_input.fileno(), child_output.fileno()
                code = (
                    'import os; '
                    'os.write(1, b"x" * 200000); '
                    'os.write(2, b"y" * 200000); '
                    f'os.write({write_fd}, b"request\\n"); '
                    f'assert os.read({read_fd}, 100) == b"reply\\n"; '
                    'os.write(1, b"\\ndone\\n")')

                async def protocol(process):
                    """Reply to process only after it has emitted its large output."""
                    child_input.close()
                    child_output.close()
                    self.assertEqual(await requests.readline(), b'request\n')
                    replies.write('reply\n')
                    replies.flush()

                await run_compiler(job, [sys.executable, '-c', code], protocol=protocol,
                                   pass_fds=(read_fd, write_fd))

        session.schedule('large', work)
        await asyncio.wait_for(session.finish(), 5)
        text = output.getvalue().split('\n', 1)[1]  # Exclude the printed command.
        self.assertEqual(text, 'x' * 200000 + 'y' * 200000 + '\ndone\n')

    async def test_reported_failure_terminates_and_reaps_other_compiler(self):
        """A job behind the failure in output order must not survive cancellation."""
        output = io.StringIO()
        session = BuildSession(jobs=2, output=output)
        ready = asyncio.Event()
        processes = []

        async def fail(job):
            """Wait for the sibling process to start before failing."""
            await ready.wait()
            raise RuntimeError('stop now')

        async def sibling(job):
            """Keep a real compiler process blocked after announcing readiness."""
            async with mapper_pipe() as (requests, replies, child_input, child_output):
                read_fd, write_fd = child_input.fileno(), child_output.fileno()
                code = (f'import os; os.write({write_fd}, b"ready\\n"); '
                        f'os.read({read_fd}, 100)')

                async def protocol(process):
                    """Record process and acknowledge that its startup finished."""
                    processes.append(process)
                    child_input.close()
                    child_output.close()
                    self.assertEqual(await requests.readline(), b'ready\n')
                    ready.set()
                    await asyncio.Event().wait()

                await run_compiler(job, [sys.executable, '-c', code], protocol=protocol,
                                   pass_fds=(read_fd, write_fd))

        session.schedule('fail', fail)
        session.schedule('sibling', sibling)
        with self.assertRaisesRegex(RuntimeError, 'stop now'):
            await asyncio.wait_for(session.finish(), 5)
        self.assertEqual(output.getvalue(), 'stop now\n')
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
