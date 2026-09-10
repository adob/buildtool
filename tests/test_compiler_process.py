"""Exercise output draining and cancellation with real, tiny child processes."""

import asyncio
import contextlib
import io
import os
import shlex
import sys
import unittest

import buildtool as bt
from compiler import mapper_pipe, run_compiler
from scheduler import BuildSession, Job


class CompilerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_verbose_echoes_silent_and_captured_commands_once(self) -> None:
        """Verbose mode echoes every launch, without duplicating commands on diagnostics."""
        for code, capture in (('pass', False), ('print("output")', False),
                              ('print("machine data")', True)):
            with self.subTest(code=code, capture=capture):
                output = io.StringIO()
                session = BuildSession(output=output, verbose=True)
                command = [sys.executable, '-c', code]

                async def work(job: Job) -> None:
                    """Run command with the selected capture mode through job."""
                    await run_compiler(job, command, capture=capture)

                session.schedule('compile', work)
                await session.finish()
                diagnostic = 'output\n' if code == 'print("output")' else ''
                self.assertEqual(output.getvalue(), 'launching ' + shlex.join(command) + '\n' + diagnostic)

    async def test_verbose_launch_bypasses_waiting_output_queue(self) -> None:
        """A later job's launch must print and flush while the reporter waits on the first."""
        launched = asyncio.Event()

        class Output(io.StringIO):
            def flush(self) -> None:
                """Acknowledge the launch only when the output stream is flushed."""
                super().flush()
                if self.getvalue().startswith('launching '):
                    launched.set()

        output = Output()
        session = BuildSession(output=output, verbose=True)
        command = [sys.executable, '-c', 'pass']

        async def first(job: Job) -> None:
            """Block job's ordered output until the later launch is already visible."""
            await launched.wait()
            self.assertEqual(output.getvalue(), 'launching ' + shlex.join(command) + '\n')
            job.message('first finished')

        async def later(job: Job) -> None:
            """Launch a silent compiler while job waits behind the first in output order."""
            await run_compiler(job, command)

        session.schedule('first', first)
        session.schedule('later', later)
        await asyncio.wait_for(session.finish(), 5)
        self.assertEqual(output.getvalue(),
                         'launching ' + shlex.join(command) + '\nfirst finished\n')

    async def test_commands_only_prefix_visible_output(self) -> None:
        """Print one command for stdout or stderr, hiding silent and captured invocations."""
        for code, capture, expected in (
            ('pass', False, ''),
            ('print("stdout")', False, 'stdout\n'),
            ('import sys; print("stderr", file=sys.stderr)', False, 'stderr\n'),
            ('print("machine data")', True, ''),
        ):
            with self.subTest(code=code, capture=capture):
                output = io.StringIO()
                session = BuildSession(output=output)
                command = [sys.executable, '-c', code]

                async def work(job: Job) -> None:
                    """Run command in job with the selected capture mode."""
                    result = await run_compiler(job, command, capture=capture)
                    if capture:
                        self.assertEqual(result.stdout, b'machine data\n')

                session.schedule('compile', work)
                await session.finish()
                prefix = shlex.join(command) + '\n' if expected else ''
                self.assertEqual(output.getvalue(), prefix + expected)

    async def test_command_precedes_live_output_before_exit(self) -> None:
        """Print command and diagnostics while the child is still waiting for a reply."""
        printed = asyncio.Event()

        class Output(io.StringIO):
            def write(self, text: str) -> int:
                """Record text and acknowledge the child's diagnostic."""
                result = super().write(text)
                if self.getvalue().endswith('warning\n'):
                    printed.set()
                return result

        output = Output()
        session = BuildSession(output=output)
        commands = []

        async def work(job: Job) -> None:
            """Hold job's compiler at a mapper request until its warning is displayed."""
            async with mapper_pipe() as (requests, replies, child_input, child_output):
                read_fd, write_fd = child_input.fileno(), child_output.fileno()
                code = (f'import os; os.write(2, b"warning\\n"); '
                        f'os.write({write_fd}, b"ready\\n"); '
                        f'os.read({read_fd}, 100)')
                command = [sys.executable, '-c', code]
                commands.append(shlex.join(command))

                async def protocol(process: asyncio.subprocess.Process) -> None:
                    """Allow process to exit only after the reporter prints its output."""
                    child_input.close()
                    child_output.close()
                    await requests.readline()
                    await printed.wait()
                    self.assertIsNone(process.returncode)
                    replies.write('continue\n')
                    replies.flush()

                await run_compiler(job, command, protocol=protocol,
                                   pass_fds=(read_fd, write_fd))

        session.schedule('warning', work)
        await asyncio.wait_for(session.finish(), 5)
        self.assertEqual(output.getvalue(), commands[0] + '\nwarning\n')

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


class SynchronousCommandTests(unittest.TestCase):
    def test_verbose_echoes_before_execution(self) -> None:
        """A silent command sees its full launch line already printed in verbose mode."""
        command = [sys.executable, '-c', 'pass']
        output = io.StringIO()
        original_run = bt.subprocess.run

        def run(*args: object, **kwargs: object) -> object:
            """Check the launch message before forwarding subprocess args and kwargs."""
            self.assertEqual(output.getvalue(), 'launching ' + shlex.join(command) + '\n')
            return original_run(*args, **kwargs)

        from unittest.mock import patch
        with contextlib.redirect_stdout(output), patch.object(bt.subprocess, 'run', side_effect=run):
            bt.shell(*command, verbose=True)
        self.assertEqual(output.getvalue(), 'launching ' + shlex.join(command) + '\n')

    def test_only_displayed_diagnostics_print_command(self) -> None:
        """Silent link-like commands and internally consumed stdout do not echo argv."""
        for code, expected_stdout, expected_stderr in (
            ('pass', '', ''),
            ('print("flags")', 'flags\n', ''),
            ('import sys; print("warning", file=sys.stderr)', '', 'warning\n'),
        ):
            with self.subTest(code=code):
                command = [sys.executable, '-c', code]
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    result = bt.shell(*command)
                self.assertEqual(result, expected_stdout)
                self.assertEqual(errors.getvalue(), expected_stderr)
                self.assertEqual(output.getvalue(),
                                 shlex.join(command) + '\n' if expected_stderr else '')
