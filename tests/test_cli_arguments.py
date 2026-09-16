"""CLI options stop at the first target/path, preserving program arguments."""

import contextlib
import io
import sys
import subprocess
import unittest
from unittest import mock

import buildtool as bt


class CliArgumentTests(unittest.TestCase):
    def test_compiler_failure_exits_with_compiler_status(self) -> None:
        """A failed compiler must never turn into a successful CLI exit."""
        error = subprocess.CalledProcessError(7, ['g++', '-c', 'broken.cc'])
        error.buildtool_reported = True
        output = io.StringIO()
        with mock.patch.object(bt, '_main', side_effect=error), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as caught:
                bt.main()
        self.assertEqual(caught.exception.code, 7)
        self.assertEqual(output.getvalue(), '')

    def test_clang_specific_configuration_is_not_used_by_gcc(self) -> None:
        """Only --clang selects its extra compilation and linking flags."""
        for flags in ([], ['--clang']):
            with self.subTest(flags=flags), mock.patch.dict(vars(bt)), \
                 mock.patch.object(sys, 'argv', ['bt', 'build', *flags, 'main.cc']), \
                 mock.patch.object(bt, 'ROOT', '.'), mock.patch.object(bt, 'build') as build:
                bt.main(vfs=bt.MemoryFileSystem(), CLANG_CXXFLAGS=['--gcc-install-dir=/sdk'],
                        CLANG_LDFLAGS=['-L/sdk/lib'])
                cfg = build.call_args.args[1]
                self.assertTrue(cfg.progress)
                self.assertEqual('--gcc-install-dir=/sdk' in cfg.CXXFLAGS, bool(flags))
                self.assertEqual('-L/sdk/lib' in cfg.LDFLAGS, bool(flags))

    def test_run_preserves_options_after_target(self) -> None:
        """Only options before the target configure the build; the rest reach execv."""
        for flags in ([], ['--debug', '--verbose', '-j', '2', '--rebuild', '--no-std-header-unit']):
            with self.subTest(flags=flags), mock.patch.dict(vars(bt)):
                fs = bt.MemoryFileSystem()
                forwarded = ['--option', 'some value', '--verbose', '--debug',
                             '--help', '--clang', '-j', '0', '--rebuild', '--no-std-header-unit', '']
                argv = ['bt', 'run', *flags, 'src/foo.cc', *forwarded]
                with mock.patch.object(sys, 'argv', argv), \
                     mock.patch.object(bt, 'ROOT', '.'), \
                     mock.patch.object(bt, 'build', return_value=bt.Path('bin/foo')) as build, \
                     mock.patch.object(bt.os, 'execv') as execute:
                    bt.main(vfs=fs)
                path, cfg = build.call_args.args
                self.assertEqual(build.call_args.kwargs, {'publish': False})
                self.assertEqual(str(path), 'src/foo.cc')
                self.assertEqual(cfg.VERBOSE, bool(flags))
                self.assertEqual(cfg.REBUILD, bool(flags))
                self.assertEqual(cfg.STD_HEADER_UNIT, not bool(flags))
                self.assertFalse(cfg.USECLANG)
                self.assertEqual(str(cfg.OBJDIR), 'build/debug' if flags else 'build/release')
                if flags:
                    self.assertEqual(cfg.JOBS, 2)
                binary = fs.abspath('bin/foo')
                execute.assert_called_once_with(binary, [binary, *forwarded])

    def test_unknown_option_before_target_is_rejected(self) -> None:
        """A typo in a buildtool option must not silently become a program argument."""
        with mock.patch.object(sys, 'argv', ['bt', 'run', '--unknown', 'src/foo.cc']), \
             contextlib.redirect_stderr(io.StringIO()), mock.patch.object(bt, 'build') as build:
            with self.assertRaises(SystemExit) as error:
                bt.main(vfs=bt.MemoryFileSystem())
        self.assertEqual(error.exception.code, 2)
        build.assert_not_called()

    def test_path_commands_stop_parsing_options(self) -> None:
        """Later option-looking arguments remain paths for test, bench, and ide."""
        for command, operation in (('test', 'run_tests'), ('bench', 'run_benchmarks'),
                                   ('ide', 'build_compilation_database')):
            with self.subTest(command=command), mock.patch.dict(vars(bt)):
                argv = ['bt', command, '--verbose', 'src', '--verbose', 'more']
                with mock.patch.object(sys, 'argv', argv), \
                     mock.patch.object(bt, 'ROOT', '.'), \
                     mock.patch.object(bt, operation) as invoke:
                    bt.main(vfs=bt.MemoryFileSystem())
                args = invoke.call_args.args
                paths, cfg = args[-2:]
                self.assertEqual(list(map(str, paths)), ['src', '--verbose', 'more'])
                self.assertTrue(cfg.VERBOSE)
