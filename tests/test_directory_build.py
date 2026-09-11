"""Directory targets compile immediate sources and publish only executable targets."""

import contextlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import buildtool as bt


class DirectoryBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        """Create a fake compiler whose source text represents its output symbol table."""
        self.fs = bt.MemoryFileSystem()
        self.fs.makedirs('cmd/foo/nested')
        self.fs.write_text('cmd/foo/main.cc', 'main T 0 10\n')
        self.fs.write_text('cmd/foo/helper.c', 'helper T 0 10\n')
        self.fs.write_text('cmd/foo/start.S', 'start T 0 10\n')
        self.fs.write_text('cmd/foo/unused.cpp', 'unused T 0 10\n')
        self.fs.write_text('cmd/foo/nested/other.cc', 'main T 0 10\n')
        self.fs.write_text('cmd/foo/api.h', '')
        self.cfg = bt.BuildConfig(vfs=self.fs, OBJDIR='build/release', CXXFLAGS=[], LDFLAGS=[])
        self.compiled = []
        self.links = []
        self.enterContext(mock.patch.object(bt.SourceFile, 'compile_gcc', autospec=True,
                                           side_effect=self.compile_source))
        self.enterContext(mock.patch.object(bt, 'shell', side_effect=self.shell))
        self.enterContext(mock.patch.object(bt, 'run_compiler', side_effect=self.run_command))

    async def run_command(self, job: bt.Job, command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        """Run scheduled inspection/link commands against the same fake toolchain."""
        async with job.compiler_slot(compilation=False):
            return subprocess.CompletedProcess(command, 0, self.shell(*command).encode(), b'')

    def compile_source(self, source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
        """Write source's simulated object using cfg; target supplies the build graph."""
        self.compiled.append(str(source.path))
        self.fs.write_text(source.objpath, self.fs.read_text(source.path))

    def shell(self, *args: object, verbose: bool = False) -> str:
        """Simulate toolchain discovery, symbol inspection, and linking from args."""
        if args[1] == '-print-prog-name=nm':
            return 'test-nm\n'
        if args[0] == 'test-nm':
            self.assertEqual(args[1:4], ('--defined-only', '--extern-only', '--format=posix'))
            return ''.join(self.fs.read_text(path) for path in args[4:])
        self.links.append(list(map(str, args)))
        output = next(str(arg)[2:] for arg in args if str(arg).startswith('-o'))
        self.fs.write_text(output, 'executable')
        return ''

    def build(self, path: str = 'cmd/foo', *, publish: bool = True) -> bt.Path | None:
        """Build path in a fresh session, optionally publishing its executable."""
        self.cfg.reset_build_state()
        with contextlib.redirect_stdout(io.StringIO()):
            return bt.build(bt.Path(path), self.cfg, publish=publish)

    def test_all_immediate_sources_link_as_directory_name(self) -> None:
        """Include unrelated source files, skip subdirectories, and reuse cached artifacts."""
        self.assertEqual(str(self.build()), 'bin/foo')
        self.assertEqual(self.compiled, ['cmd/foo/helper.c', 'cmd/foo/main.cc',
                                        'cmd/foo/start.S', 'cmd/foo/unused.cpp'])
        self.assertEqual(self.fs.readlink('bin/foo'), '../build/release/bin/foo')
        self.assertEqual(sum(arg.endswith('.o') for arg in self.links[0]), 4)
        self.build()
        self.assertEqual(len(self.compiled), 4)
        self.assertEqual(len(self.links), 1)
        self.fs.write_text('cmd/foo/added.cc', 'added T 0 10\n')
        self.build()
        self.assertEqual(self.compiled[-1], 'cmd/foo/added.cc')
        self.assertEqual(len(self.links), 2)

    def test_no_main_compiles_without_publishing(self) -> None:
        """A namespaced or undefined main is insufficient to produce an executable."""
        self.fs.write_text('cmd/foo/main.cc', '_ZN3foo4mainEv T 0 10\nmain U\n')
        self.assertIsNone(self.build())
        self.assertEqual(len(self.compiled), 4)
        self.assertEqual(self.links, [])
        self.assertFalse(self.fs.is_dir('bin'))

    def test_explicit_sources_are_not_directory_roots(self) -> None:
        """BUILD.py may reserve alternative runners for explicitly selected targets."""
        self.fs.write_text('cmd/foo/BUILD.py', 'EXPLICIT_SOURCES = ["main.cc"]\n')
        self.assertIsNone(self.build())
        self.assertNotIn('cmd/foo/main.cc', self.compiled)
        self.assertEqual(str(self.build('cmd/foo/main.cc')), 'bin/main')

    def test_internal_binary_name_and_explicit_override(self) -> None:
        """Directory runs use the internal artifact; suffixes and explicit names still apply."""
        self.cfg.SUFFIX = '+debug'
        self.assertEqual(str(self.build(publish=False)), 'build/release/bin/foo+debug')
        self.assertFalse(self.fs.is_dir('bin'))
        self.cfg.OUTFILE = 'custom'
        self.assertEqual(str(self.build()), 'bin/custom')

    def test_empty_directory_reports_error(self) -> None:
        """Reject an input directory that contains no supported source files."""
        self.fs.makedirs('empty')
        with self.assertRaisesRegex(RuntimeError, 'No source files in empty'):
            self.build('empty')

    def test_explicit_file_keeps_file_name(self) -> None:
        """An explicit main.cc target still produces main, not its directory name."""
        self.assertEqual(str(self.build('cmd/foo/main.cc')), 'bin/main')

    def test_recursive_pattern_builds_independent_targets(self) -> None:
        """Recursive directories with main link separately and exclude test sources."""
        self.fs.write_text('cmd/foo/broken_test.cc', 'this test must not be built')
        with contextlib.redirect_stdout(io.StringIO()):
            bt.build_targets(bt.Path('cmd/foo/...'), self.cfg)
        self.assertEqual(len(self.links), 2)
        self.assertNotIn('cmd/foo/broken_test.cc', self.compiled)
        self.assertEqual(self.compiled[-1], 'cmd/foo/nested/other.cc')
        self.assertFalse(any('nested/other.o' in arg for arg in self.links[0]))
        self.assertTrue(any('nested/other.o' in arg for arg in self.links[1]))

    def test_recursive_build_reports_concurrency_once(self) -> None:
        """Share the banner across packages, including when early packages are cached."""
        self.cfg.memory = bt.MemoryBudget(available=lambda: 8 * 1024**3)

        def compile_source(source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
            """Emit a compiler status for source and simulate its object output."""
            source.job.message(f'BUILDING {source.path}')
            self.compile_source(source, target, cfg)

        with mock.patch.object(bt.SourceFile.compile_gcc, 'side_effect', compile_source):
            for state in ('fresh', 'cached', 'changed'):
                with self.subTest(state=state):
                    self.cfg.reset_build_state()
                    if state == 'changed':
                        self.fs.write_text('cmd/foo/nested/other.cc', 'main T 0 20\n')
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        bt.build_targets(bt.Path('cmd/foo/...'), self.cfg)
                    text = output.getvalue()
                    self.assertEqual(text.count('Concurrency:'), 0 if state == 'cached' else 1)
                    if state != 'cached':
                        self.assertLess(text.index('Concurrency:'), text.index('BUILDING'))

    def test_recursive_same_basename_has_distinct_artifacts(self) -> None:
        """Different packages named foo must not accidentally reuse the same binary."""
        self.fs.makedirs('other/foo')
        self.fs.write_text('other/foo/main.cc', 'main T 0 10\n')
        with contextlib.redirect_stdout(io.StringIO()):
            bt.build_targets(bt.Path('cmd/...'), self.cfg)
            bt.build_targets(bt.Path('other/...'), self.cfg)
        outputs = [next(arg for arg in link if arg.startswith('-o')) for link in self.links]
        self.assertEqual(len(outputs), len(set(outputs)))

    def test_directory_without_main_cannot_run(self) -> None:
        """CLI run reports a missing entry point without attempting exec or publishing."""
        self.fs.write_text('cmd/foo/main.cc', '')
        with mock.patch.dict(vars(bt)), mock.patch.object(bt, 'ROOT', '.'), \
             mock.patch.object(sys, 'argv', ['bt', 'run', 'cmd/foo']), \
             mock.patch.object(bt.os, 'execv') as execute, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bt.main(vfs=self.fs)
        execute.assert_not_called()
        self.assertEqual(self.links, [])


class DirectoryCompilerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
    def test_recursive_build_and_test_packages(self) -> None:
        """Real packages with identical basenames and symbols must build and run independently."""
        with tempfile.TemporaryDirectory(prefix='buildtool-recursive-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            self.addCleanup(os.chdir, previous)
            Path('runner.cc').write_text('int check(); int main() { return check(); }\n')
            for package in ('packages/one/same', 'packages/two/same'):
                Path(package).mkdir(parents=True)
                Path(package, 'main.cc').write_text('int main() { return 0; }\n')
                Path(package, 'marker').write_text('fixture')
                Path(package, 'case_test.cc').write_text(
                    'extern "C" int access(const char *, int);\n'
                    'int check() { return access("marker", 0); }\n')
            cfg = bt.BuildConfig(CXX=os.environ['BT_TEST_GCC'], CXXFLAGS=['-std=c++20'],
                                 LDFLAGS=shlex.split(os.environ.get('BT_TEST_GCC_LDFLAGS', '')),
                                 STD_HEADER_UNIT=False, JOBS=2)
            with mock.patch.object(bt, 'ROOT', directory), mock.patch.object(bt, 'TESTMAIN', 'runner.cc'), \
                 contextlib.redirect_stdout(io.StringIO()):
                bt.build_targets(bt.Path('packages/...'), cfg)
                binaries = [path for path in Path('build/release/packages').glob('*/same') if path.is_file()]
                self.assertEqual(len(binaries), 2)
                for binary in binaries:
                    subprocess.run([str(binary.resolve())], check=True)
                bt.run_tests(['packages/...'], cfg)
                self.assertEqual(len(list(Path('build/release/tests').glob('*/same'))), 2)
                Path('packages/one/same/case_test.cc').write_text('int check() { return 1; }\n')
                cfg.reset_build_state()
                # Preserve actual execution while counting the packages run after a failure.
                with mock.patch.object(bt.subprocess, 'run', wraps=subprocess.run) as execute, \
                     self.assertRaises(SystemExit):
                    bt.run_tests(['packages/...'], cfg)
                tests = [call for call in execute.call_args_list if 'cwd' in call.kwargs]
                self.assertEqual(len(tests), 2)

    def exercise(self, compiler: str, wrapper: str | None = None, ldflags: str = '') -> None:
        """Compile a mixed C/C++ directory with compiler and optional wrapper/linker flags."""
        with tempfile.TemporaryDirectory(prefix='buildtool-directory-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                Path('foo').mkdir()
                Path('foo/main.cc').write_text(
                    '#define ENTRY main\nextern "C" int helper();\n'
                    'int ENTRY() { return helper() != 42; }\n')
                Path('foo/helper.c').write_text('int helper(void) { return 42; }\n')
                cfg = bt.BuildConfig(CXX=compiler, CXXFLAGS=['-std=c++20'], CFLAGS=[],
                                     LDFLAGS=shlex.split(ldflags), JOBS=2,
                                     USECLANG=bool(wrapper), CLANG_WRAPPER=wrapper)
                with contextlib.redirect_stdout(io.StringIO()):
                    binary = bt.build(bt.Path('foo'), cfg)
                self.assertEqual(str(binary), 'bin/foo')
                self.assertEqual(subprocess.run([os.path.abspath(binary)]).returncode, 0)
                self.assertTrue(Path('build/release/foo/helper.o').is_file())
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    bt.build(bt.Path('foo'), cfg)
                self.assertEqual(output.getvalue(), '')

                Path('support').mkdir()
                Path('support/code.cc').write_text('#if 0\nint main() {}\n#endif\nint library() { return 1; }\n')
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(bt.build(bt.Path('support'), cfg))
                self.assertFalse(Path('bin/support').exists())
            finally:
                os.chdir(previous)

    @unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
    def test_gcc_directory(self) -> None:
        """Check directory builds and object-symbol detection with GCC."""
        self.exercise(os.environ['BT_TEST_GCC'], ldflags=os.environ.get('BT_TEST_GCC_LDFLAGS', ''))

    @unittest.skipUnless(os.environ.get('BT_TEST_CLANG') and os.environ.get('BT_TEST_CLANG_WRAPPER'),
                         'set Clang test environment variables')
    def test_clang_directory(self) -> None:
        """Check directory builds and object-symbol detection with the Clang wrapper."""
        self.exercise(os.environ['BT_TEST_CLANG'], os.environ['BT_TEST_CLANG_WRAPPER'],
                      os.environ.get('BT_TEST_CLANG_LDFLAGS', ''))
