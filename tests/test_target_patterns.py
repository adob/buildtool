"""Go-style directory selection and separate test executables per package."""

import contextlib
import io
import subprocess
import sys
import unittest
from unittest import mock

import buildtool as bt


class PatternFixture(unittest.TestCase):
    def setUp(self) -> None:
        """Create nested source/test directories using an isolated filesystem."""
        self.fs = bt.MemoryFileSystem(cwd='/workspace')
        for directory in ('pkg', 'pkg/sub', 'pkg/empty/deep'):
            self.fs.makedirs(directory)
            self.fs.write_text(directory + '/code.cc', '')
            self.fs.write_text(directory + '/code_test.cc', '')
        self.cfg = bt.BuildConfig(vfs=self.fs, OBJDIR='build/release', DEPDIR='build/release')
        self.enterContext(mock.patch.object(bt, 'ROOT', '/workspace'))


class TargetPatternTests(PatternFixture):
    def test_expansion_requires_explicit_ellipsis(self) -> None:
        """A directory stays one target, while /... visits source-bearing descendants."""
        self.assertEqual(bt.expand_target_pattern(bt.Path('pkg'), self.cfg), [bt.Path('pkg')])
        expected = ['pkg', 'pkg/empty/deep', 'pkg/sub']
        self.assertEqual(list(map(str, bt.expand_target_pattern(bt.Path('pkg/...'), self.cfg))), expected)
        self.assertEqual(list(map(str, bt.expand_target_pattern(bt.Path('pkg/.../'), self.cfg, tests=True))), expected)

    def test_recursive_exclusions(self) -> None:
        """Skip generated outputs, fixtures, hidden directories, and directory symlinks."""
        for directory in ('.hidden', '_private', 'testdata', 'vendor', 'build/debug', 'bin'):
            self.fs.makedirs(directory)
            self.fs.write_text(directory + '/ignored.cc', '')
        self.fs.symlink('pkg', 'alias')
        self.assertEqual(list(map(str, bt.expand_target_pattern(bt.Path('./...'), self.cfg))),
                         ['pkg', 'pkg/empty/deep', 'pkg/sub'])

    def test_invalid_and_empty_patterns(self) -> None:
        """Reject nonexistent roots and diagnose patterns with no matching sources."""
        with self.assertRaisesRegex(RuntimeError, 'Not a directory'):
            bt.expand_target_pattern(bt.Path('missing/...'), self.cfg)
        self.fs.makedirs('empty')
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(bt.expand_target_pattern(bt.Path('empty/...'), self.cfg), [])
        self.assertIn('matched no source directories', output.getvalue())

    def test_cli_build_expands_before_building(self) -> None:
        """The build command dispatches one directory at a time for a recursive pattern."""
        with mock.patch.dict(vars(bt)), mock.patch.object(sys, 'argv', ['bt', 'build', 'pkg/...']), \
             mock.patch.object(bt, 'build') as build:
            bt.main(vfs=self.fs)
        self.assertEqual([str(call.args[0]) for call in build.call_args_list],
                         ['pkg', 'pkg/empty/deep', 'pkg/sub'])


class TestPackageTests(PatternFixture):
    def setUp(self) -> None:
        """Mock compiler/linker work and process execution while exercising real selection."""
        super().setUp()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.target = self.enterContext(mock.patch.object(bt, 'Target'))
        self.target.return_value.link.side_effect = lambda **kwargs: kwargs['artifact']
        self.execute = self.enterContext(mock.patch.object(bt.subprocess, 'run',
            return_value=subprocess.CompletedProcess([], 0)))

    def test_directory_is_nonrecursive(self) -> None:
        """An ordinary directory runs its immediate tests in that directory."""
        bt.run_tests(['pkg'], self.cfg)
        sources = self.target.return_value.compile_many.call_args.args[0]
        self.assertEqual(list(map(str, sources)), [bt.TESTMAIN, 'pkg/code_test.cc'])
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(self.execute.call_args.kwargs['cwd'], '/workspace/pkg')
        self.assertFalse(self.target.return_value.link.call_args.kwargs['publish'])

    def test_recursive_tests_run_separate_binaries(self) -> None:
        """Separate directories get separate selections, output paths, and working directories."""
        bt.run_tests(['pkg/...'], self.cfg)
        compilations = self.target.return_value.compile_many.call_args_list
        self.assertEqual(len(compilations), 3)
        self.assertTrue(all(len(call.args[0]) == 2 for call in compilations))
        self.assertEqual([call.kwargs['cwd'] for call in self.execute.call_args_list],
                         ['/workspace/pkg', '/workspace/pkg/empty/deep', '/workspace/pkg/sub'])
        binaries = [call.args[0][0] for call in self.execute.call_args_list]
        self.assertEqual(len(set(binaries)), 3)
        self.assertEqual(self.fs.getcwd(), '/workspace')

    def test_overlapping_patterns_do_not_duplicate_tests(self) -> None:
        """Repeated paths, explicit files, and recursive matches are deduplicated."""
        bt.run_tests(['pkg', 'pkg/code_test.cc', 'pkg/...'], self.cfg)
        self.assertEqual(self.execute.call_count, 3)

    def test_explicit_file_selection_has_distinct_binary(self) -> None:
        """Selecting fewer tests cannot reuse a package binary containing extra tests."""
        self.fs.write_text('pkg/extra_test.cpp', '')
        bt.run_tests(['pkg'], self.cfg)
        whole = self.execute.call_args.args[0]
        bt.run_tests(['pkg/code_test.cc'], self.cfg)
        self.assertNotEqual(whole, self.execute.call_args.args[0])

    def test_failure_does_not_skip_later_packages(self) -> None:
        """Return failure overall but execute packages after an unsuccessful test."""
        self.execute.side_effect = [subprocess.CompletedProcess([], 1),
                                   subprocess.CompletedProcess([], 0),
                                   subprocess.CompletedProcess([], 0)]
        with self.assertRaises(SystemExit) as error:
            bt.run_tests(['pkg/...'], self.cfg)
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.execute.call_count, 3)
        self.assertEqual(self.fs.getcwd(), '/workspace')

    def test_compile_failure_does_not_skip_later_packages(self) -> None:
        """A package that cannot build is reported without executing its stale binary."""
        self.target.return_value.compile_many.side_effect = [RuntimeError('compile failed'), None, None]
        with self.assertRaises(SystemExit):
            bt.run_tests(['pkg/...'], self.cfg)
        self.assertEqual(self.execute.call_count, 2)

    def test_no_test_files_launches_nothing(self) -> None:
        """A directory without tests does not execute an old test artifact."""
        self.fs.makedirs('no-tests')
        bt.run_tests(['no-tests'], self.cfg)
        self.target.assert_not_called()
        self.execute.assert_not_called()

    def test_invocation_from_subdirectory(self) -> None:
        """Resolve arguments from the caller but exclude output paths relative to ROOT."""
        self.fs.makedirs('build/debug')
        self.fs.write_text('build/debug/generated_test.cc', '')
        self.fs.chdir('pkg')
        bt.run_tests(['../...'], self.cfg)
        self.assertEqual(self.execute.call_count, 3)
        self.assertEqual(self.fs.getcwd(), '/workspace/pkg')
