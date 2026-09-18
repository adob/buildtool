"""Imports identify module interfaces through ordered filename conventions."""

import contextlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

import buildtool as bt


class ModuleLookupTests(unittest.TestCase):
    def test_lookup_failure_identifies_source_before_compilation(self) -> None:
        """A dependency lookup during cache checking still names its importing source."""
        self.fs.write_text('main.cc', '')
        output = io.StringIO()

        async def build(source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
            """Simulate dependency validation without launching a compiler."""
            target.mod2src('missing', bt.SourceType.MODULE)

        with mock.patch.object(bt.SourceFile, 'build', build), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'Unable to locate module missing'):
                self.target.compile_many([bt.Path('main.cc')])
        self.assertIn('main.cc: error: Unable to locate module missing', output.getvalue())

    def test_tagged_interface_requires_active_tags(self) -> None:
        """Find a qualified interface only when every filename tag is active."""
        self.fs.write_text('deps/base/lib/math/math+zephyr+posix.cc', '')
        self.cfg.TAGS = {'zephyr'}
        with self.assertRaisesRegex(RuntimeError, 'Unable to locate module'):
            self.target.mod2src('lib.math', bt.SourceType.MODULE)
        self.cfg.TAGS.add('posix')
        self.assertEqual(self.target.mod2src('lib.math', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/math+zephyr+posix.cc'))

    def test_ambiguous_tagged_interfaces_and_unqualified_priority(self) -> None:
        """Reject two active variants unless an unqualified interface takes priority."""
        self.fs.write_text('deps/base/lib/math/math+linux.cc', '')
        self.fs.write_text('deps/base/lib/math/math+posix.cc', '')
        self.cfg.TAGS = {'linux', 'posix'}
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous module lib.math'):
            self.target.mod2src('lib.math', bt.SourceType.MODULE)
        self.fs.write_text('deps/base/lib/math/math.cc', '')
        self.assertEqual(self.target.mod2src('lib.math', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/math.cc'))

    def setUp(self) -> None:
        """Create independent virtual search roots and a target using them."""
        self.fs = bt.MemoryFileSystem()
        self.cfg = bt.BuildConfig(vfs=self.fs, SRCDIR='.', INCFLAGS=['-Ideps/base'])
        self.target = bt.Target(bt.Path('main'), self.cfg)
        self.fs.makedirs('deps/base/lib/math')

    def test_all_layouts_without_source_inspection(self) -> None:
        """Locate each conventional path using only filesystem metadata."""
        for relative in ('lib/math.cc', 'lib/math/math.cc', 'lib/math/module.cc'):
            with self.subTest(path=relative):
                fs = bt.MemoryFileSystem()
                fs.makedirs('deps/base/lib/math')
                fs.write_text('deps/base/' + relative, 'contents are deliberately not C++')
                target = bt.Target(bt.Path('main'), bt.BuildConfig(
                    vfs=fs, SRCDIR='.', INCFLAGS=['-Ideps/base']))
                with mock.patch.object(fs, 'read_text', side_effect=AssertionError('source read')), \
                     mock.patch.object(fs, 'scandir', side_effect=AssertionError('directory scan')):
                    self.assertEqual(target.mod2src('lib.math', bt.SourceType.MODULE),
                                     bt.Path('deps/base/' + relative))

    def test_direct_path_takes_precedence(self) -> None:
        """Keep the existing direct module path ahead of the directory fallback."""
        self.fs.write_text('deps/base/lib/math.cc', 'export module lib.math;')
        self.fs.write_text('deps/base/lib/math/math.cc', '')
        self.fs.write_text('deps/base/lib/math/module.cc', '')
        self.assertEqual(self.target.mod2src('lib.math', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math.cc'))

    def test_module_filename_precedes_basename(self) -> None:
        """The module.cc fallback has priority over the nested basename."""
        self.fs.write_text('deps/base/lib/math/math.cc', '')
        self.fs.write_text('deps/base/lib/math/module.cc', '')
        self.assertEqual(self.target.mod2src('lib.math', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/module.cc'))

    def test_search_root_order_is_preserved(self) -> None:
        """Try every layout in an earlier root before moving to the next root."""
        self.fs.makedirs('lib/math')
        self.fs.write_text('lib/math/module.cc', '')
        self.fs.write_text('deps/base/lib/math.cc', '')
        self.assertEqual(self.target.mod2src('lib.math', bt.SourceType.MODULE),
                         bt.Path('lib/math/module.cc'))

    def test_lookup_does_not_recurse(self) -> None:
        """Nested directories belong to other module paths."""
        self.fs.makedirs('deps/base/lib/math/nested')
        self.fs.write_text('deps/base/lib/math/nested/api.cc', 'export module lib.math;')
        self.fs.write_text('deps/base/lib/math/public.cc', 'export module lib.math;')
        with self.assertRaisesRegex(RuntimeError, 'Unable to locate module lib.math'):
            self.target.mod2src('lib.math', bt.SourceType.MODULE)

    def test_directory_named_like_source_is_skipped(self) -> None:
        """Only regular files can satisfy a conventional module candidate."""
        self.fs.makedirs('deps/base/lib/math.cc')
        self.fs.write_text('deps/base/lib/math/module.cc', '')
        self.assertEqual(self.target.mod2src('lib.math', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/module.cc'))

    def test_header_lookup_keeps_exact_path(self) -> None:
        """Header-unit lookup continues using the requested header path."""
        self.fs.write_text('deps/base/lib/math/math.h', '')
        self.assertEqual(self.target.mod2src('lib/math/math.h', bt.SourceType.USER_HEADER),
                         bt.Path('deps/base/lib/math/math.h'))

    def test_header_does_not_use_module_fallback(self) -> None:
        """A header import must not resolve to the directory's module.cc file."""
        self.fs.write_text('deps/base/lib/math/module.cc', '')
        with self.assertRaisesRegex(RuntimeError, 'Unable to locate module'):
            self.target.mod2src('lib/math.h', bt.SourceType.USER_HEADER)

    def test_partition_variant_maps_dots_to_filename_tags(self) -> None:
        """Dots in a partition select +tag source variants without changing module paths."""
        self.fs.write_text('deps/base/lib/math/core+zephyr.cc', '')
        self.fs.write_text('deps/base/lib/math/core+zephyr+debug.cc', '')
        self.assertEqual(bt.mod2path('lib.math:core.zephyr', bt.SourceType.MODULE),
                         bt.Path('lib/math/core+zephyr.cc'))
        self.assertEqual(bt.mod2path('lib.math:core.zephyr.debug', bt.SourceType.MODULE),
                         bt.Path('lib/math/core+zephyr+debug.cc'))
        self.assertEqual(self.target.mod2src('lib.math:core.zephyr', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/core+zephyr.cc'))
        self.assertEqual(self.target.mod2src('lib.math:core.zephyr.debug', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/core+zephyr+debug.cc'))

    def test_partition_falls_back_to_tagged_primary_source(self) -> None:
        """A partition may select a +tag variant beside the primary module source."""
        self.fs.write_text('deps/base/lib/math+zephyr.cc', '')
        self.fs.write_text('deps/base/lib/math+zephyr+debug.cc', '')
        self.cfg.TAGS = {'linux'}
        self.assertEqual(self.target.mod2src('lib.math:zephyr', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math+zephyr.cc'))
        self.assertEqual(self.target.mod2src('lib.math:zephyr.debug', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math+zephyr+debug.cc'))

    def test_structural_partition_precedes_tagged_primary_fallback(self) -> None:
        """Keep the conventional partition layout ahead of the +tag primary layout."""
        self.fs.write_text('deps/base/lib/math/zephyr.cc', '')
        self.fs.write_text('deps/base/lib/math+zephyr.cc', '')
        self.assertEqual(self.target.mod2src('lib.math:zephyr', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/zephyr.cc'))

    def test_explicit_partition_variant_ignores_active_source_tags(self) -> None:
        """An explicit partition variant remains addressable even when its +tag is inactive."""
        path = bt.Path('deps/base/lib/math/core+zephyr.cc')
        self.fs.write_text(str(path), '')
        self.cfg.TAGS = {'linux'}
        self.cfg.KNOWN_TAGS = {'linux'}
        self.assertEqual(self.target.mod2src('lib.math:core.zephyr', bt.SourceType.MODULE), path)
        source = bt.SourceFile.get(path, self.cfg, type=bt.SourceType.MODULE,
                                   modname='lib.math:core.zephyr')
        self.assertEqual(source.path, path)

        tagged_primary = bt.Path('deps/base/lib/math+zephyr.cc')
        self.fs.write_text(str(tagged_primary), '')
        self.assertEqual(self.target.mod2src('lib.math:zephyr', bt.SourceType.MODULE), tagged_primary)
        source = bt.SourceFile.get(tagged_primary, self.cfg, type=bt.SourceType.MODULE,
                                   modname='lib.math:zephyr')
        self.assertEqual(source.path, tagged_primary)

    def test_ordinary_partition_keeps_existing_path_mapping(self) -> None:
        """Partitions without dots continue mapping directly to a same-directory .cc file."""
        self.fs.write_text('deps/base/lib/math/core.cc', '')
        self.assertEqual(bt.mod2path('lib.math:core', bt.SourceType.MODULE),
                         bt.Path('lib/math/core.cc'))
        self.assertEqual(self.target.mod2src('lib.math:core', bt.SourceType.MODULE),
                         bt.Path('deps/base/lib/math/core.cc'))


class ModuleLookupCompilerTests(unittest.TestCase):
    def exercise(self, compiler: str, wrapper: str | None = None, ldflags: str = '') -> None:
        """Build all module layouts using compiler, optional wrapper, and linker flags."""
        with tempfile.TemporaryDirectory(prefix='buildtool-module-lookup-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                Path('lib/math').mkdir(parents=True)
                Path('lib/math/math.h').write_text('inline int legacy() { return 2; }\n')
                Path('main.cc').write_text('#include "lib/math/math.h"\nimport lib.math;\n'
                                           'int main() { return answer() + legacy() != 42; }\n')
                for filename in ('lib/math.cc', 'lib/math/math.cc', 'lib/math/module.cc'):
                    with self.subTest(filename=filename):
                        interface = Path(filename)
                        interface.write_text('export module lib.math;\n'
                                             'export int answer() { return 40; }\n')
                        cfg = bt.BuildConfig(CXX=compiler, CXXFLAGS=['-std=c++20'],
                                             LDFLAGS=shlex.split(ldflags), INCFLAGS=['-I.'],
                                             OBJDIR='build/' + filename, JOBS=2,
                                             USECLANG=bool(wrapper), CLANG_WRAPPER=wrapper)
                        with contextlib.redirect_stdout(io.StringIO()):
                            binary = bt.build(bt.Path('main.cc'), cfg, publish=False)
                        self.assertEqual(subprocess.run([os.path.abspath(binary)]).returncode, 0)
                        self.assertEqual(cfg.source_files[bt.Path(interface)].type, bt.SourceType.MODULE)
                        cfg.reset_build_state()
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            bt.build(bt.Path('main.cc'), cfg, publish=False)
                        self.assertEqual(output.getvalue(), '')
                        interface.unlink()
            finally:
                os.chdir(previous)

    @unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
    def test_gcc(self) -> None:
        """Exercise lookup and legacy header coexistence with GCC."""
        self.exercise(os.environ['BT_TEST_GCC'], ldflags=os.environ.get('BT_TEST_GCC_LDFLAGS', ''))

    @unittest.skipUnless(os.environ.get('BT_TEST_CLANG') and os.environ.get('BT_TEST_CLANG_WRAPPER'),
                         'set Clang test environment variables')
    def test_clang(self) -> None:
        """Exercise lookup and legacy header coexistence with the patched Clang wrapper."""
        self.exercise(os.environ['BT_TEST_CLANG'], os.environ['BT_TEST_CLANG_WRAPPER'],
                      os.environ.get('BT_TEST_CLANG_LDFLAGS', ''))
