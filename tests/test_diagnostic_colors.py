"""Captured compiler diagnostics retain colors when reported to a terminal."""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from clang_mapper import compile_with_mapper_async
from compiler import diagnostic_color_flags, run_compiler
from scheduler import BuildSession, Job


class DiagnosticColorTests(unittest.TestCase):
    def test_terminal_detection_and_explicit_preferences(self) -> None:
        """Force colors only when the final output and user preferences allow it."""
        output = io.StringIO()
        with mock.patch.dict(os.environ, TERM='xterm', NO_COLOR=''):
            self.assertEqual(diagnostic_color_flags(output, ['g++']), [])
            with mock.patch.object(output, 'isatty', return_value=True):
                self.assertEqual(diagnostic_color_flags(output, ['g++']),
                                 ['-fdiagnostics-color=always'])
                for flag in ('-fdiagnostics-color=never', '-fdiagnostics-color=always',
                             '-fdiagnostics-color=auto', '-fno-color-diagnostics',
                             '-fcolor-diagnostics', '-fno-diagnostics-color'):
                    self.assertEqual(diagnostic_color_flags(output, ['clang++', flag]), [])
                with mock.patch.dict(os.environ, NO_COLOR='1'):
                    self.assertEqual(diagnostic_color_flags(output, ['g++']), [])
                with mock.patch.dict(os.environ, TERM='dumb'):
                    self.assertEqual(diagnostic_color_flags(output, ['g++']), [])


class CompilerColorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def check_compiler(self, compiler: str, wrapper: str | None = None) -> None:
        """Check colored/plain diagnostics from compiler, optionally via wrapper."""
        with tempfile.TemporaryDirectory(prefix='buildtool-color-') as directory:
            source = Path(directory) / 'warning.cc'
            source.write_text('#warning diagnostic-color-probe\n')
            for terminal in (False, True):
                output = io.StringIO()
                session = BuildSession(output=output)

                async def resolve(request: object) -> str:
                    """Reject unexpected module requests from this import-free source."""
                    raise AssertionError(f'unexpected import: {request}')

                async def work(job: Job) -> None:
                    """Run a warning-producing compilation through job's output buffer."""
                    command = [compiler, '-fsyntax-only', str(source)]
                    if wrapper:
                        await compile_with_mapper_async(job, wrapper, command, resolve)
                    else:
                        await run_compiler(job, command, color_diagnostics=True)

                with mock.patch.object(output, 'isatty', return_value=terminal), \
                     mock.patch.dict(os.environ, TERM='xterm', NO_COLOR='',
                                     GCC_COLORS='warning=01;35'):
                    session.schedule('warning', work)
                    await session.finish()
                diagnostics = output.getvalue().split('\n', 1)[1]
                self.assertIn('diagnostic-color-probe', diagnostics)
                self.assertEqual('\x1b[' in diagnostics, terminal)

    @unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
    async def test_gcc_colors(self) -> None:
        """Check ANSI diagnostics from GCC through the subprocess runner."""
        await self.check_compiler(os.environ['BT_TEST_GCC'])

    @unittest.skipUnless(os.environ.get('BT_TEST_CLANG_WRAPPER') and
                         os.environ.get('BT_TEST_CLANG'), 'set Clang test environment variables')
    async def test_clang_wrapper_colors(self) -> None:
        """Check ANSI diagnostics from the embedded Clang wrapper."""
        await self.check_compiler(os.environ['BT_TEST_CLANG'], os.environ['BT_TEST_CLANG_WRAPPER'])
