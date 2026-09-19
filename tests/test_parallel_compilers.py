"""Opt-in GCC and patched-Clang tests for the same parallel module graph."""

import contextlib
from collections.abc import Sequence
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import buildtool as bt


class ParallelCompilerTests(unittest.TestCase):
    def exercise(
        self,
        compiler: str,
        *,
        wrapper: str | None = None,
        ldflags: Sequence[str] = (),
        absolute_header: bool = False,
    ) -> None:
        """Build with compiler/wrapper/ldflags; absolute_header uses an absolute include root."""
        with tempfile.TemporaryDirectory(prefix='buildtool-parallel-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                Path('main.cc').write_text(
                    '#include "a.h"\n#include "b.h"\n'
                    'int main() { return a() + b() != 14; }\n')
                Path('a.h').write_text('int a();\n')
                Path('b.h').write_text('int b();\n')
                header_import = 'import <shared.h>;' if absolute_header else 'import "shared.h";'
                for name in ('a', 'b'):
                    Path(name + '.cc').write_text(
                        f'import value;\n{header_import}\n'
                        f'int {name}() {{ return value() + shared(); }}\n')
                Path('value.cc').write_text(
                    'export module value;\nexport int value() { return 5; }\n')
                Path('shared.h').write_text('inline int shared() { return 2; }\n')
                cfg = bt.BuildConfig(CXX=compiler, CXXFLAGS=['-std=c++20'],
                                     CFLAGS=[], LDFLAGS=list(ldflags),
                                     INCFLAGS=['-I' + (directory if absolute_header else '.')],
                                     USECLANG=bool(wrapper), CLANG_WRAPPER=wrapper,
                                     JOBS=2)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    binary = bt.build(bt.Path('main.cc'), cfg)
                self.assertEqual(subprocess.run([os.path.abspath(binary)]).returncode, 0)
                if absolute_header:
                    header = next(source for source in cfg.source_files.values()
                                  if source.path.name == 'shared.h')
                    self.assertTrue(header.cmpath.is_file(cfg.vfs))
                    self.assertFalse(Path('shared.h.pcm').exists())
                completed = [line for line in output.getvalue().splitlines()
                             if line.startswith('BUILT ')]
                self.assertEqual(len(completed), 5)
                for path in ('main.cc', 'a.cc', 'b.cc'):
                    self.assertTrue(any(path in line for line in completed))
                # Clang requests header units during preprocessing, before
                # its parser asks for named modules. GCC's encounter order differs.
                self.assertTrue(any('value.cc' in line for line in completed))
                self.assertTrue(any('shared.h' in line for line in completed))
                before = {p: p.stat().st_mtime_ns for p in Path('build').rglob('*')
                          if p.is_file()}
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    bt.build(bt.Path('main.cc'), cfg)
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
                cfg.REBUILD = True
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    bt.build(bt.Path('main.cc'), cfg)
                rebuilt = [line for line in output.getvalue().splitlines()
                           if line.startswith('BUILT ')]
                self.assertCountEqual(rebuilt, completed)
                self.assertIn('LINKING ', output.getvalue())
                self.assertEqual(subprocess.run([os.path.abspath(binary)]).returncode, 0)
                cfg.REBUILD = False
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    bt.build(bt.Path('main.cc'), cfg)
                self.assertEqual(output.getvalue(), '')
                Path('shared.h').write_text('inline int shared() { return 3; }\n')
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()):
                    bt.build(bt.Path('main.cc'), cfg)
                self.assertEqual(subprocess.run([os.path.abspath(binary)]).returncode, 1)
                for name in ('one', 'two'):
                    Path('apps', name).mkdir(parents=True)
                    Path('apps', name, 'main.cc').write_text(
                        '#include "../../a.h"\n#include "../../b.h"\n'
                        'int main() { return a() + b() != 16; }\n')
                cfg.REBUILD = True
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    bt.build_targets(bt.Path('apps/...'), cfg)
                completed = [line for line in output.getvalue().splitlines()
                             if line.startswith('BUILT ')]
                self.assertEqual(len(completed), 6)
                self.assertEqual(sum('value.cc' in line for line in completed), 1)
                self.assertEqual(sum('shared.h' in line for line in completed), 1)
                for name in ('one', 'two'):
                    subprocess.run([os.path.abspath('bin/' + name)], check=True)
                cfg.REBUILD = False
                cfg.reset_build_state()
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    bt.build_targets(bt.Path('apps/...'), cfg)
                self.assertEqual(output.getvalue(), '')
            finally:
                os.chdir(previous)

    @unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
    def test_gcc_shared_modules(self):
        """Use the GCC mapper with concurrent importers and incremental rebuilds."""
        self.exercise(os.environ['BT_TEST_GCC'],
                      ldflags=shlex.split(os.environ.get('BT_TEST_GCC_LDFLAGS', '')))

    @unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
    def test_gcc_absolute_header_unit_stays_in_build_directory(self):
        """An absolute header identity must not put the PCM beside the input header."""
        self.exercise(os.environ['BT_TEST_GCC'], absolute_header=True,
                      ldflags=shlex.split(os.environ.get('BT_TEST_GCC_LDFLAGS', '')))

    @unittest.skipUnless(os.environ.get('BT_TEST_CLANG_WRAPPER') and
                         os.environ.get('BT_TEST_CLANG'),
                         'set BT_TEST_CLANG_WRAPPER and BT_TEST_CLANG for Clang tests')
    def test_clang_shared_modules(self):
        """Use the patched Clang mapper with the same graph as GCC."""
        self.exercise(os.environ['BT_TEST_CLANG'],
                      wrapper=os.environ['BT_TEST_CLANG_WRAPPER'],
                      ldflags=shlex.split(os.environ.get('BT_TEST_CLANG_LDFLAGS', '')))
