"""CLI options stop at the first target/path, preserving program arguments."""

import contextlib
import io
import sys
import unittest
from unittest import mock

import buildtool as bt


class CliArgumentTests(unittest.TestCase):
    def test_run_preserves_options_after_target(self) -> None:
        """Only options before the target configure the build; the rest reach execv."""
        for flags in ([], ['--debug', '--verbose', '-j', '2', '--rebuild']):
            with self.subTest(flags=flags), mock.patch.dict(vars(bt)):
                fs = bt.MemoryFileSystem()
                forwarded = ['--option', 'some value', '--verbose', '--debug',
                             '--help', '--clang', '-j', '0', '--rebuild', '']
                argv = ['bt', 'run', *flags, 'src/foo.cc', *forwarded]
                with mock.patch.object(sys, 'argv', argv), \
                     mock.patch.object(bt, 'ROOT', '.'), \
                     mock.patch.object(bt, 'build', return_value=bt.Path('bin/foo')) as build, \
                     mock.patch.object(bt.os, 'execv') as execute:
                    bt.main(vfs=fs)
                path, cfg = build.call_args.args
                self.assertEqual(str(path), 'src/foo.cc')
                self.assertEqual(cfg.VERBOSE, bool(flags))
                self.assertEqual(cfg.REBUILD, bool(flags))
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
